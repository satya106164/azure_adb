"""Business operations. Every state change happens here, inside one transaction,
with authorisation, segregation-of-duties checks and an audit record.

The App calls these as the signed-in user; jobs call the shared helpers
(`mark_for_revoke`, `owners_for_group`, ...) with a system actor.
"""
from __future__ import annotations

import functools
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import psycopg

from . import audit, naming, notify, templates, ucsql, workflow
from .config import get_settings
from .db import fetch_all, transaction
from .models import (
    AppRole, ApproverRole, CurrentUser, GrantStatus, GroupPurpose, PrivilegeLevel,
    RequestStatus, RequestType, RevokeReason,
)
from .workflow import ApprovalContext, ApprovalRule

log = logging.getLogger(__name__)

LIVE_GRANT_STATES = ("PENDING_PROVISION", "ACTIVE", "PENDING_REVOKE")


class ServiceError(Exception):
    """A user-facing, expected failure (validation, authorisation, state)."""


def _service(fn: Callable) -> Callable:
    """Translate domain errors into ServiceError so the UI can show them plainly."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ServiceError:
            raise
        except (workflow.TransitionError, workflow.ConfigurationError, naming.NamingError,
                templates.TemplateError, ucsql.GrantSqlError) as exc:
            raise ServiceError(str(exc)) from exc
        except psycopg.errors.UniqueViolation as exc:
            raise ServiceError("That already exists (or there is already an open request for it).") from exc
        except psycopg.errors.CheckViolation as exc:
            raise ServiceError(f"The data failed a validation rule: {exc.diag.constraint_name}") from exc
        except psycopg.errors.InsufficientPrivilege as exc:
            log.warning("Database refused %s for this app: %s", fn.__name__, exc)
            raise ServiceError("This app is not permitted to make that change. "
                               "Use the Data access app or the Catalog admin app as appropriate.") from exc
    return wrapper


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require(user: CurrentUser, *roles: AppRole) -> None:
    if not any(user.has(r) for r in roles):
        names = " or ".join(r.value.lower().replace("_", " ") for r in roles)
        raise ServiceError(f"You need the {names} role to do this.")


def _one(cur, sql: str, params: Any = None) -> dict | None:
    cur.execute(sql, params)
    return cur.fetchone()


def _must(row: dict | None, what: str) -> dict:
    if row is None:
        raise ServiceError(f"{what} was not found.")
    return row


# ---------------------------------------------------------------------------
# Shared lookups (also used by jobs)
# ---------------------------------------------------------------------------
def ensure_user(cur, email: str, display_name: str | None = None) -> dict:
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ServiceError(f"{email!r} is not a valid email address.")
    cur.execute(
        """
        INSERT INTO app_user (email, display_name, last_seen_at) VALUES (%s, %s, now())
        ON CONFLICT (email) DO UPDATE
          SET display_name = coalesce(EXCLUDED.display_name, app_user.display_name)
        RETURNING *
        """,
        (email, display_name),
    )
    return cur.fetchone()


def load_rules(cur) -> list[ApprovalRule]:
    cur.execute("SELECT * FROM approval_rule ORDER BY classification_code, stage_no")
    return [ApprovalRule(r["stage_no"], r["classification_code"], ApproverRole(r["approver_role"]), r["sla_hours"])
            for r in cur.fetchall()]


def classification(cur, code: str) -> dict:
    return _must(_one(cur, "SELECT * FROM classification WHERE classification_code = %s", (code,)), "Classification")


def get_group(cur, access_group_id) -> dict:
    return _must(_one(cur, "SELECT * FROM v_group WHERE access_group_id = %s", (access_group_id,)), "Access group")


def owners_for_group(cur, group: dict) -> dict[ApproverRole, set[str]]:
    """Owners and stewards for a group, most specific object first.

    Order: the group itself -> its AI use case or project -> its data product.
    The first object that has holders for a role wins. DATA_OWNER falls back to
    the group owner.
    """
    candidates: list[tuple[str, Any]] = [("ACCESS_GROUP", group["access_group_id"])]
    if group.get("ai_use_case_id"):
        candidates.append(("AI_USE_CASE", group["ai_use_case_id"]))
    if group.get("project_id"):
        candidates.append(("PROJECT", group["project_id"]))
    if group.get("effective_data_product_id"):
        candidates.append(("DATA_PRODUCT", group["effective_data_product_id"]))
    cur.execute(
        """
        SELECT o.object_kind, o.object_id, o.role, u.email
        FROM ownership o JOIN app_user u ON u.user_id = o.user_id
        WHERE o.valid_to IS NULL AND u.is_active AND o.object_id = ANY(%s)
        """,
        ([c[1] for c in candidates],),
    )
    rows = cur.fetchall()
    out: dict[ApproverRole, set[str]] = {}
    for role in ("DATA_OWNER", "STEWARD"):
        for kind, oid in candidates:
            holders = {r["email"] for r in rows if r["object_kind"] == kind and r["object_id"] == oid and r["role"] == role}
            if holders:
                out[ApproverRole(role)] = holders
                break
    if not out.get(ApproverRole.DATA_OWNER) and group.get("owner_email"):
        out[ApproverRole.DATA_OWNER] = {group["owner_email"]}
    return out


def active_delegations(cur) -> dict[str, set[str]]:
    cur.execute(
        """
        SELECT a.email AS delegator, b.email AS delegate
        FROM delegation d
        JOIN app_user a ON a.user_id = d.delegator_user_id
        JOIN app_user b ON b.user_id = d.delegate_user_id
        WHERE now() BETWEEN d.valid_from AND d.valid_to AND b.is_active
        """
    )
    out: dict[str, set[str]] = {}
    for r in cur.fetchall():
        out.setdefault(r["delegator"], set()).add(r["delegate"])
    return out


def approval_context(cur, request: dict, group: dict) -> ApprovalContext:
    """request is a v_request row."""
    holders = owners_for_group(cur, group)
    project_id = request.get("project_id") or group.get("project_id")
    if project_id is None and request.get("project_code"):
        row = _one(cur, "SELECT project_id FROM project WHERE code = %s", (request["project_code"],))
        project_id = row and row["project_id"]
    if project_id:
        sponsor = _one(cur, "SELECT u.email FROM project p JOIN app_user u ON u.user_id = p.sponsor_user_id "
                            "WHERE p.project_id = %s", (project_id,))
        if sponsor:
            holders[ApproverRole.PROJECT_SPONSOR] = {sponsor["email"]}
    cur.execute(
        "SELECT u.email FROM approval_task t JOIN app_user u ON u.user_id = t.decided_by_user_id "
        "WHERE t.request_id = %s AND t.status = 'APPROVED' AND NOT t.is_post_hoc",
        (request["request_id"],),
    )
    prior = {r["email"] for r in cur.fetchall()}
    return ApprovalContext(
        requester=request["requester_email"], beneficiary=request["beneficiary_email"],
        role_holders=holders, delegations=active_delegations(cur), prior_approvers=prior,
    )


def _settings_tz():
    s = get_settings()
    return s.holidays, s.timezone


def _create_task(cur, request_id, rule: ApprovalRule, now: datetime, *, is_post_hoc: bool = False,
                 due_at: datetime | None = None) -> dict:
    holidays, tz = _settings_tz()
    due = due_at or workflow.add_business_hours(now, rule.sla_hours, holidays, tz)
    cur.execute(
        "INSERT INTO approval_task (request_id, stage_no, approver_role, is_post_hoc, due_at) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (request_id, rule.stage_no, rule.approver_role.value, is_post_hoc, due),
    )
    return cur.fetchone()


def _describe(req: dict) -> str:
    target = req.get("data_product_name") or req.get("ai_use_case_code") or req.get("project_code") or ""
    lines = [
        f"Reference: {req['request_ref']}",
        f"Access: {req['group_name']} ({req['privilege_level']}) {target}".rstrip(),
        f"Classification: {req['classification_code']}",
        f"Requested by: {req['requester_email']}",
        f"For: {req['beneficiary_email']}",
    ]
    if req.get("project_code"):
        lines.append(f"Project: {req['project_code']}")
    lines.append(f"Justification: {req['justification']}")
    return "\n".join(lines)


def _notify_stage(cur, req: dict, rule: ApprovalRule, ctx: ApprovalContext, due: datetime) -> None:
    subject = f"Approval needed: {req['request_ref']} ({req['classification_code']})"
    body = _describe(req) + f"\n\nStage {rule.stage_no}: {rule.approver_role.value}. Due {due:%d %b %Y %H:%M} UTC."
    if rule.approver_role == ApproverRole.SECURITY:
        notify.security_alert(cur, subject, body, "APPROVAL_NEEDED", request_id=req["request_id"],
                              dedupe_key=f"approval:{req['request_id']}:{rule.stage_no}")
        return
    recipients = workflow.eligible_approvers(rule.approver_role, ctx)
    if not recipients:
        notify.admin_alert(cur, f"No eligible approver for {req['request_ref']}",
                           body + "\n\nAssign an owner/steward or a delegate so the request can proceed.",
                           "NO_APPROVER", dedupe_key=f"noapprover:{req['request_id']}:{rule.stage_no}")
        return
    notify.enqueue(cur, recipients.keys(), subject, body, "APPROVAL_NEEDED", request_id=req["request_id"],
                   dedupe_key=f"approval:{req['request_id']}:{rule.stage_no}")


def _v_request(cur, request_id) -> dict:
    return _must(_one(cur, "SELECT * FROM v_request WHERE request_id = %s", (request_id,)), "Request")


def _set_request_status(cur, request_id, status: RequestStatus, **extra) -> dict:
    sets = ["status = %s", "version = version + 1"]
    params: list[Any] = [status.value]
    for col, val in extra.items():
        sets.append(f"{col} = %s")
        params.append(val)
    params.append(request_id)
    cur.execute(f"UPDATE access_request SET {', '.join(sets)} WHERE request_id = %s RETURNING *", params)
    return cur.fetchone()


def mark_for_revoke(cur, grant: dict, reason: RevokeReason, actor: CurrentUser | str,
                    note: str | None = None) -> dict:
    """ACTIVE -> PENDING_REVOKE. The provisioning job removes the membership."""
    new = workflow.next_grant_status(grant["status"], "revoke")
    actor_name = actor.email if isinstance(actor, CurrentUser) else str(actor)
    cur.execute(
        "UPDATE access_grant SET status = %s, revoke_reason = %s, revoke_requested_at = now(), revoked_by = %s "
        "WHERE grant_id = %s RETURNING *",
        (new.value, reason.value, actor_name, grant["grant_id"]),
    )
    after = cur.fetchone()
    audit.record(cur, actor, "GRANT_REVOKE_REQUESTED", "ACCESS_GRANT", grant["grant_id"],
                 before=grant, after=after, reason=note or reason.value, correlation_id=grant["request_id"])
    who = _one(cur, "SELECT u.email, g.group_name FROM app_user u, access_group g "
                    "WHERE u.user_id = %s AND g.access_group_id = %s", (grant["user_id"], grant["access_group_id"]))
    if who:
        notify.enqueue(cur, [who["email"]], f"Access is being removed: {who['group_name']}",
                       f"Your membership of {who['group_name']} is being removed. Reason: {reason.value}."
                       + (f"\nNote: {note}" if note else ""), "ACCESS_REVOKING",
                       request_id=grant["request_id"], dedupe_key=f"revoking:{grant['grant_id']}")
    return after


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def _validate_project(cur, project_id, group: dict) -> dict:
    project = _must(_one(cur, "SELECT * FROM project WHERE project_id = %s", (project_id,)), "Project")
    if project["status"] != "ACTIVE":
        raise ServiceError(f"Project {project['code']} is not active.")
    if project["end_date"] and project["end_date"] < date.today():
        raise ServiceError(f"Project {project['code']} ended on {project['end_date']}.")
    if group["purpose"] == GroupPurpose.PROJECT.value and group["project_id"] != project_id:
        raise ServiceError("This group belongs to a different project.")
    dp_id = group.get("effective_data_product_id")
    if dp_id and not _one(cur, "SELECT 1 FROM project_data_product WHERE project_id = %s AND data_product_id = %s",
                          (project_id, dp_id)):
        raise ServiceError(f"Project {project['code']} is not approved to use data product "
                           f"{group['data_product_code']}. Ask the data owner or an admin to link them.")
    return project


@_service
def submit_request(user: CurrentUser, *, access_group_id, justification: str, duration_days: int,
                   project_id=None, beneficiary_email: str | None = None,
                   request_type: RequestType = RequestType.GRANT, extends_grant_id=None) -> dict:
    justification = (justification or "").strip()
    if len(justification) < 20:
        raise ServiceError("Give a business justification of at least 20 characters.")
    rtype = RequestType(request_type)
    if rtype == RequestType.BREAK_GLASS:
        raise ServiceError("Use the break-glass page for emergency access.")
    with transaction() as cur:
        requester = ensure_user(cur, user.email, user.display_name)
        beneficiary = ensure_user(cur, beneficiary_email or user.email)
        if not beneficiary["is_active"]:
            raise ServiceError(f"{beneficiary['email']} is not an active user.")
        group = get_group(cur, access_group_id)
        if group["status"] not in ("PLANNED", "ACTIVE"):
            raise ServiceError(f"{group['group_name']} is {group['status'].lower()} and cannot be requested.")
        cls = classification(cur, group["classification_code"])

        if rtype == RequestType.EXTEND:
            grant = _must(_one(cur, "SELECT * FROM access_grant WHERE grant_id = %s FOR UPDATE",
                               (extends_grant_id,)), "Grant")
            if (grant["status"] != GrantStatus.ACTIVE.value or grant["user_id"] != beneficiary["user_id"]
                    or grant["access_group_id"] != group["access_group_id"]):
                raise ServiceError("Only your own active access can be extended.")
            if grant["is_break_glass"]:
                raise ServiceError("Break-glass access cannot be extended; raise a normal request.")
            if project_id is None:
                orig = _one(cur, "SELECT project_id FROM access_request WHERE request_id = %s", (grant["request_id"],))
                project_id = orig and orig["project_id"]
        else:
            if not group["requestable"]:
                raise ServiceError(f"{group['group_name']} is not open for self-service requests.")
            if _one(cur, "SELECT 1 FROM access_grant WHERE user_id = %s AND access_group_id = %s AND status = ANY(%s)",
                    (beneficiary["user_id"], access_group_id, list(LIVE_GRANT_STATES))):
                raise ServiceError("This person already has (or is being given) this access. "
                                   "Use Extend on the My access page instead.")

        if cls["requires_project"] and not project_id:
            raise ServiceError(f"{cls['classification_code'].title()} data can only be requested for a project.")
        if project_id:
            _validate_project(cur, project_id, group)

        stages = workflow.plan_stages(load_rules(cur), group["classification_code"], rtype)
        duration = max(1, min(int(duration_days or cls["max_grant_days"]), cls["max_grant_days"]))
        cur.execute(
            """
            INSERT INTO access_request (request_type, requester_user_id, beneficiary_user_id, access_group_id,
                project_id, extends_grant_id, classification_code, justification, requested_duration_days, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'DRAFT') RETURNING *
            """,
            (rtype.value, requester["user_id"], beneficiary["user_id"], access_group_id, project_id,
             extends_grant_id, group["classification_code"], justification, duration),
        )
        draft = cur.fetchone()
        now = _now()
        status = workflow.next_request_status(draft["status"], "submit", "requester")
        _set_request_status(cur, draft["request_id"], status, current_stage_no=1, submitted_at=now)
        task = _create_task(cur, draft["request_id"], stages[0], now)
        req = _v_request(cur, draft["request_id"])
        audit.record(cur, user, "REQUEST_SUBMITTED", "ACCESS_REQUEST", draft["request_id"],
                     after=draft, correlation_id=req["request_ref"])
        _notify_stage(cur, req, stages[0], approval_context(cur, req, group), task["due_at"])
        return req


@_service
def request_extension(user: CurrentUser, grant_id, duration_days: int, justification: str) -> dict:
    with transaction() as cur:
        grant = _must(_one(cur, "SELECT * FROM v_grant WHERE grant_id = %s", (grant_id,)), "Grant")
    if grant["user_email"] != user.email:
        raise ServiceError("You can only extend your own access.")
    return submit_request(user, access_group_id=grant["access_group_id"], justification=justification,
                          duration_days=duration_days, request_type=RequestType.EXTEND, extends_grant_id=grant_id)


@_service
def decide_task(user: CurrentUser, task_id, approve: bool, comment: str | None = None) -> dict:
    comment = (comment or "").strip()
    if not approve and len(comment) < 5:
        raise ServiceError("Say why you are rejecting (at least 5 characters).")
    with transaction() as cur:
        task = _must(_one(cur, "SELECT * FROM approval_task WHERE task_id = %s FOR UPDATE", (task_id,)), "Task")
        if task["status"] != "PENDING":
            raise ServiceError("This approval has already been decided.")
        raw = _one(cur, "SELECT * FROM access_request WHERE request_id = %s FOR UPDATE", (task["request_id"],))
        req = _v_request(cur, raw["request_id"])
        group = get_group(cur, raw["access_group_id"])
        ctx = approval_context(cur, {**req, "project_id": raw["project_id"]}, group)
        ok, reason, on_behalf = workflow.can_decide(user.email, user.app_roles, task["approver_role"], ctx)
        if not ok:
            raise ServiceError(reason)
        decider = ensure_user(cur, user.email, user.display_name)
        behalf = ensure_user(cur, on_behalf)["user_id"] if on_behalf else None
        now = _now()
        cur.execute(
            "UPDATE approval_task SET status = %s, decided_by_user_id = %s, on_behalf_of_user_id = %s, "
            "decided_at = %s, decision_comment = %s WHERE task_id = %s RETURNING *",
            ("APPROVED" if approve else "REJECTED", decider["user_id"], behalf, now, comment or None, task_id),
        )
        decided = cur.fetchone()
        audit.record(cur, user, "TASK_APPROVED" if approve else "TASK_REJECTED", "APPROVAL_TASK", task_id,
                     before=task, after=decided, reason=comment or None, correlation_id=req["request_ref"])

        if task["is_post_hoc"]:  # break-glass review
            if not approve:
                grant = _one(cur, "SELECT * FROM access_grant WHERE request_id = %s FOR UPDATE", (raw["request_id"],))
                if grant and grant["status"] == GrantStatus.ACTIVE.value:
                    mark_for_revoke(cur, grant, RevokeReason.POST_HOC_REJECTED, user, comment)
                notify.security_alert(cur, f"Break-glass use rejected on review: {req['request_ref']}",
                                      _describe(req) + f"\n\nReviewer: {user.email}\nComment: {comment}",
                                      "BREAK_GLASS_REJECTED", request_id=raw["request_id"])
            return decided

        if not approve:
            status = workflow.next_request_status(raw["status"], "reject", "approver")
            _set_request_status(cur, raw["request_id"], status, decided_at=now)
            audit.record(cur, user, "REQUEST_REJECTED", "ACCESS_REQUEST", raw["request_id"],
                         reason=comment, correlation_id=req["request_ref"])
            notify.enqueue(cur, [req["requester_email"], req["beneficiary_email"]],
                           f"Request rejected: {req['request_ref']}",
                           _describe(req) + f"\n\nRejected by {user.email}: {comment}", "REQUEST_REJECTED",
                           request_id=raw["request_id"])
            return decided

        stages = workflow.plan_stages(load_rules(cur), raw["classification_code"], raw["request_type"])
        remaining = [s for s in stages if s.stage_no > task["stage_no"]]
        if remaining:
            status = workflow.next_request_status(raw["status"], "approve_stage", "approver")
            _set_request_status(cur, raw["request_id"], status, current_stage_no=remaining[0].stage_no)
            new_task = _create_task(cur, raw["request_id"], remaining[0], now)
            ctx.prior_approvers.add(user.email)
            _notify_stage(cur, req, remaining[0], ctx, new_task["due_at"])
        else:
            _finalise_approval(cur, user, raw, req, now)
        return decided


def _finalise_approval(cur, user: CurrentUser, raw: dict, req: dict, now: datetime) -> None:
    status = workflow.next_request_status(raw["status"], "approve_final", "approver")
    cls = classification(cur, raw["classification_code"])
    project_end = None
    if raw["project_id"]:
        project_end = _one(cur, "SELECT end_date FROM project WHERE project_id = %s", (raw["project_id"],))["end_date"]
    valid_to = workflow.effective_valid_to(now, raw["requested_duration_days"], cls["max_grant_days"], project_end)

    if raw["request_type"] == RequestType.EXTEND.value:
        grant = _one(cur, "SELECT * FROM access_grant WHERE grant_id = %s FOR UPDATE", (raw["extends_grant_id"],))
        if not grant or grant["status"] != GrantStatus.ACTIVE.value:
            _set_request_status(cur, raw["request_id"], RequestStatus.CANCELLED, decided_at=now)
            audit.record(cur, user, "REQUEST_CANCELLED", "ACCESS_REQUEST", raw["request_id"],
                         reason="Access being extended is no longer active", correlation_id=req["request_ref"])
            return
        new_to = max(valid_to, grant["valid_to"])
        cur.execute("UPDATE access_grant SET valid_to = %s, expiry_notice_level = 0, last_recertified_at = %s "
                    "WHERE grant_id = %s RETURNING *", (new_to, now, grant["grant_id"]))
        after = cur.fetchone()
        status = workflow.next_request_status(status, "extended", "approver")
        _set_request_status(cur, raw["request_id"], status, decided_at=now, fulfilled_at=now)
        audit.record(cur, user, "GRANT_EXTENDED", "ACCESS_GRANT", grant["grant_id"], before=grant, after=after,
                     correlation_id=req["request_ref"])
        notify.enqueue(cur, [req["beneficiary_email"]], f"Access extended: {req['group_name']}",
                       f"Your access now runs until {new_to:%d %b %Y}.", "ACCESS_EXTENDED",
                       request_id=raw["request_id"])
        return

    _set_request_status(cur, raw["request_id"], status, decided_at=now)
    cur.execute(
        "INSERT INTO access_grant (request_id, user_id, access_group_id, valid_from, valid_to, last_recertified_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (raw["request_id"], raw["beneficiary_user_id"], raw["access_group_id"], now, valid_to, now),
    )
    grant = cur.fetchone()
    audit.record(cur, user, "REQUEST_APPROVED", "ACCESS_REQUEST", raw["request_id"], correlation_id=req["request_ref"])
    audit.record(cur, user, "GRANT_CREATED", "ACCESS_GRANT", grant["grant_id"], after=grant,
                 correlation_id=req["request_ref"])
    notify.enqueue(cur, [req["requester_email"], req["beneficiary_email"]],
                   f"Request approved: {req['request_ref']}",
                   _describe(req) + f"\n\nApproved. Access is being set up and runs until {valid_to:%d %b %Y}.",
                   "REQUEST_APPROVED", request_id=raw["request_id"])


@_service
def cancel_request(user: CurrentUser, request_id) -> None:
    with transaction() as cur:
        raw = _must(_one(cur, "SELECT * FROM access_request WHERE request_id = %s FOR UPDATE", (request_id,)), "Request")
        req = _v_request(cur, request_id)
        if user.email in (req["requester_email"], req["beneficiary_email"]):
            actor_kind = "requester"
        elif user.has(AppRole.ADMIN):
            actor_kind = "admin"
        else:
            raise ServiceError("Only the requester, the person it is for, or an admin can cancel this request.")
        status = workflow.next_request_status(raw["status"], "cancel", actor_kind)
        _set_request_status(cur, request_id, status, decided_at=_now())
        cur.execute("UPDATE approval_task SET status = 'CANCELLED' WHERE request_id = %s AND status = 'PENDING'",
                    (request_id,))
        audit.record(cur, user, "REQUEST_CANCELLED", "ACCESS_REQUEST", request_id, before=raw,
                     correlation_id=req["request_ref"])


@_service
def break_glass(user: CurrentUser, access_group_id, incident_ref: str, justification: str) -> dict:
    _require(user, AppRole.BREAK_GLASS)
    incident_ref = (incident_ref or "").strip()
    justification = (justification or "").strip()
    if not incident_ref or len(justification) < 20:
        raise ServiceError("An incident reference and a justification (20+ characters) are required.")
    settings = get_settings()
    with transaction() as cur:
        me = ensure_user(cur, user.email, user.display_name)
        group = get_group(cur, access_group_id)
        if group["status"] != "ACTIVE":
            raise ServiceError("Break-glass needs an active group.")
        if _one(cur, "SELECT 1 FROM access_grant WHERE user_id = %s AND access_group_id = %s AND status = ANY(%s)",
                (me["user_id"], access_group_id, list(LIVE_GRANT_STATES))):
            raise ServiceError("You already have this access.")
        now = _now()
        cur.execute(
            """
            INSERT INTO access_request (request_type, requester_user_id, beneficiary_user_id, access_group_id,
                classification_code, justification, incident_ref, status)
            VALUES ('BREAK_GLASS', %s, %s, %s, %s, %s, %s, 'DRAFT') RETURNING *
            """,
            (me["user_id"], me["user_id"], access_group_id, group["classification_code"], justification, incident_ref),
        )
        draft = cur.fetchone()
        status = workflow.next_request_status(draft["status"], "break_glass", "break_glass")
        _set_request_status(cur, draft["request_id"], status, submitted_at=now, decided_at=now)
        valid_to = workflow.effective_valid_to(now, None, 1, request_type=RequestType.BREAK_GLASS,
                                               break_glass_hours=settings.break_glass_hours)
        cur.execute(
            "INSERT INTO access_grant (request_id, user_id, access_group_id, is_break_glass, valid_from, valid_to) "
            "VALUES (%s, %s, %s, true, %s, %s) RETURNING *",
            (draft["request_id"], me["user_id"], access_group_id, now, valid_to),
        )
        grant = cur.fetchone()
        review = ApprovalRule(1, group["classification_code"], ApproverRole.SECURITY, 1)
        _create_task(cur, draft["request_id"], review, now, is_post_hoc=True,
                     due_at=now + timedelta(hours=settings.break_glass_review_hours))
        req = _v_request(cur, draft["request_id"])
        audit.record(cur, user, "BREAK_GLASS_INVOKED", "ACCESS_GRANT", grant["grant_id"], after=grant,
                     reason=f"{incident_ref}: {justification}", correlation_id=req["request_ref"])
        notify.security_alert(cur, f"Break-glass access used: {group['group_name']}",
                              _describe(req) + f"\nIncident: {incident_ref}\nExpires: {valid_to:%d %b %Y %H:%M} UTC."
                              "\nA security reviewer must review this within "
                              f"{settings.break_glass_review_hours} hours.",
                              "BREAK_GLASS_INVOKED", request_id=draft["request_id"])
        return req


@_service
def revoke_grant(user: CurrentUser, grant_id, note: str | None = None) -> dict:
    with transaction() as cur:
        grant = _must(_one(cur, "SELECT * FROM access_grant WHERE grant_id = %s FOR UPDATE", (grant_id,)), "Grant")
        if grant["status"] != GrantStatus.ACTIVE.value:
            raise ServiceError("Only active access can be removed.")
        grantee = _one(cur, "SELECT email FROM app_user WHERE user_id = %s", (grant["user_id"],))["email"]
        group = get_group(cur, grant["access_group_id"])
        if user.email == grantee:
            reason = RevokeReason.SELF
        elif user.has(AppRole.ADMIN):
            reason = RevokeReason.ADMIN
        elif user.email in owners_for_group(cur, group).get(ApproverRole.DATA_OWNER, set()):
            reason = RevokeReason.OWNER
        else:
            raise ServiceError("Only the person, the data owner or an admin can remove this access.")
        return mark_for_revoke(cur, grant, reason, user, note)


@_service
def tasks_for_user(user: CurrentUser) -> list[dict]:
    """Pending approval tasks this user may decide (incl. as delegate)."""
    out = []
    with transaction() as cur:
        cur.execute("SELECT * FROM v_task WHERE task_status = 'PENDING' ORDER BY due_at")
        tasks = cur.fetchall()
        groups: dict[Any, dict] = {}
        for t in tasks:
            gid = t["access_group_id"]
            groups.setdefault(gid, get_group(cur, gid))
            ctx = approval_context(cur, t, groups[gid])
            ok, _, on_behalf = workflow.can_decide(user.email, user.app_roles, t["approver_role"], ctx)
            if ok:
                out.append({**t, "on_behalf_of": on_behalf})
    return out


# ---------------------------------------------------------------------------
# Recertification decisions
# ---------------------------------------------------------------------------
@_service
def recert_decide(user: CurrentUser, item_id, keep: bool, comment: str | None = None) -> None:
    comment = (comment or "").strip()
    if not keep and len(comment) < 5:
        raise ServiceError("Say why the access should be removed.")
    with transaction() as cur:
        item = _must(_one(cur, "SELECT i.*, u.email AS reviewer_email FROM recert_item i "
                               "JOIN app_user u ON u.user_id = i.reviewer_user_id "
                               "WHERE i.item_id = %s FOR UPDATE OF i", (item_id,)), "Review item")
        if item["decision"] != "PENDING":
            raise ServiceError("This item has already been reviewed.")
        delegates = active_delegations(cur).get(item["reviewer_email"], set())
        if user.email != item["reviewer_email"] and user.email not in delegates:
            raise ServiceError("You are not the reviewer (or their delegate) for this item.")
        grant = _one(cur, "SELECT * FROM access_grant WHERE grant_id = %s FOR UPDATE", (item["grant_id"],))
        grantee = _one(cur, "SELECT email FROM app_user WHERE user_id = %s", (grant["user_id"],))["email"]
        if user.email == grantee:
            raise ServiceError("You cannot review your own access.")
        cur.execute("UPDATE recert_item SET decision = %s, decided_by = %s, decided_at = now(), decision_comment = %s "
                    "WHERE item_id = %s RETURNING *",
                    ("KEEP" if keep else "REVOKE", user.email, comment or None, item_id))
        after = cur.fetchone()
        audit.record(cur, user, "RECERT_DECIDED", "RECERT_ITEM", item_id, before=item, after=after,
                     correlation_id=grant["request_id"])
        if grant["status"] == GrantStatus.ACTIVE.value:
            if keep:
                cur.execute("UPDATE access_grant SET last_recertified_at = now() WHERE grant_id = %s",
                            (grant["grant_id"],))
            else:
                mark_for_revoke(cur, grant, RevokeReason.RECERT_REVOKE, user, comment)


# ---------------------------------------------------------------------------
# Reference data administration
# ---------------------------------------------------------------------------
def _upsert_securable(cur, securable_type: str, full_name: str, data_product_id=None,
                      classification_code: str = "GENERAL") -> dict:
    full_name = full_name.strip().lower()
    ucsql.quote_securable(securable_type, full_name)  # validates shape and characters
    cur.execute(
        """
        INSERT INTO uc_securable (securable_type, full_name, data_product_id, classification_code)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (full_name) DO UPDATE
          SET data_product_id = coalesce(uc_securable.data_product_id, EXCLUDED.data_product_id)
        RETURNING *
        """,
        (securable_type, full_name, data_product_id, classification_code),
    )
    return cur.fetchone()


def _assign_ownership(cur, actor: CurrentUser, kind: str, object_id, email: str, role: str) -> None:
    u = ensure_user(cur, email)
    cur.execute(
        "INSERT INTO ownership (object_kind, object_id, user_id, role, assigned_by) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (object_kind, object_id, user_id, role) WHERE valid_to IS NULL DO NOTHING RETURNING *",
        (kind, object_id, u["user_id"], role, actor.email),
    )
    row = cur.fetchone()
    if row:
        audit.record(cur, actor, "OWNERSHIP_ASSIGNED", kind, object_id, after=row)


def _plan_group(cur, actor: CurrentUser, purpose: GroupPurpose, level: PrivilegeLevel, code: str,
                classification_code: str, owner_user_id, *, requestable: bool = True,
                description: str | None = None, **links) -> dict:
    name = naming.group_name(get_settings().env, purpose, code, level)
    cols = ["group_name", "purpose", "privilege_level", "classification_code", "owner_user_id",
            "requestable", "description", "created_by"] + list(links)
    vals = [name, purpose.value, level.value, classification_code, owner_user_id, requestable,
            description, actor.email] + list(links.values())
    cur.execute(f"INSERT INTO access_group ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
                vals)
    group = cur.fetchone()
    audit.record(cur, actor, "GROUP_PLANNED", "ACCESS_GROUP", group["access_group_id"], after=group)
    return group


def _add_entitlement_row(cur, actor: CurrentUser, access_group_id, privilege: str, *,
                         securable_id=None, asset_id=None) -> dict:
    cur.execute(
        """
        INSERT INTO group_entitlement (access_group_id, securable_id, asset_id, privilege, created_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (access_group_id, (coalesce(securable_id, asset_id)), privilege) DO UPDATE
          SET desired_state = 'PRESENT',
              apply_status = CASE WHEN group_entitlement.desired_state = 'PRESENT'
                                   AND group_entitlement.apply_status = 'APPLIED'
                                  THEN 'APPLIED' ELSE 'PENDING' END
        RETURNING *
        """,
        (access_group_id, securable_id, asset_id, privilege, actor.email),
    )
    row = cur.fetchone()
    audit.record(cur, actor, "ENTITLEMENT_SET", "GROUP_ENTITLEMENT", row["entitlement_id"], after=row)
    return row


@_service
def register_workspace(user: CurrentUser, *, name: str, workspace_type: str, host_url: str,
                       environment: str, databricks_workspace_id: int | None = None) -> dict:
    _require(user, AppRole.ADMIN)
    with transaction() as cur:
        cur.execute(
            "INSERT INTO workspace (name, workspace_type, host_url, environment, databricks_workspace_id, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (name.strip(), workspace_type, host_url.strip().rstrip("/"), environment, databricks_workspace_id,
             user.email),
        )
        row = cur.fetchone()
        audit.record(cur, user, "WORKSPACE_REGISTERED", "WORKSPACE", row["workspace_id"], after=row)
        return row


@_service
def register_data_product(user: CurrentUser, *, code: str, name: str, description: str | None,
                          catalog: str, schema: str, classification_code: str, owner_email: str,
                          steward_email: str, home_workspace_id=None, logical_groups: Iterable[str] = (),
                          create_groups: bool = True) -> dict:
    _require(user, AppRole.ADMIN)
    naming.validate_code(code)
    if classification_code != "GENERAL" and owner_email.strip().lower() == steward_email.strip().lower():
        raise ServiceError("Data owner and steward must be different people for confidential or restricted data.")
    with transaction() as cur:
        cur.execute(
            "INSERT INTO data_product (code, name, description, catalog_name, schema_name, classification_code, "
            "home_workspace_id, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (code, name, description, catalog.lower(), schema.lower(), classification_code, home_workspace_id,
             user.email),
        )
        dp = cur.fetchone()
        cat = _upsert_securable(cur, "CATALOG", catalog)
        sch = _upsert_securable(cur, "SCHEMA", f"{catalog}.{schema}", dp["data_product_id"], classification_code)
        for lg in logical_groups:
            cur.execute("INSERT INTO securable_logical_group (securable_id, logical_group_code) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING", (sch["securable_id"], lg))
        _assign_ownership(cur, user, "DATA_PRODUCT", dp["data_product_id"], owner_email, "DATA_OWNER")
        _assign_ownership(cur, user, "DATA_PRODUCT", dp["data_product_id"], steward_email, "STEWARD")
        owner = ensure_user(cur, owner_email)
        if create_groups:
            for level in (PrivilegeLevel.READ, PrivilegeLevel.WRITE):
                group = _plan_group(cur, user, GroupPurpose.DATA_PRODUCT, level, code, classification_code,
                                    owner["user_id"], requestable=(level == PrivilegeLevel.READ),
                                    description=f"{level.value.title()} access to data product {name}",
                                    data_product_id=dp["data_product_id"])
                for kind, priv in templates.data_product_entitlements(level):
                    target = cat if kind == "CATALOG" else sch
                    _add_entitlement_row(cur, user, group["access_group_id"], priv, securable_id=target["securable_id"])
        audit.record(cur, user, "DATA_PRODUCT_REGISTERED", "DATA_PRODUCT", dp["data_product_id"], after=dp)
        return dp


@_service
def register_project(user: CurrentUser, *, code: str, name: str, description: str | None, sponsor_email: str,
                     workspace_id=None, end_date: date | None = None, classification_code: str = "GENERAL",
                     create_group: bool = True) -> dict:
    _require(user, AppRole.ADMIN)
    naming.validate_code(code)
    with transaction() as cur:
        sponsor = ensure_user(cur, sponsor_email)
        cur.execute(
            "INSERT INTO project (code, name, description, sponsor_user_id, workspace_id, end_date, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (code, name, description, sponsor["user_id"], workspace_id, end_date, user.email),
        )
        project = cur.fetchone()
        if create_group:
            _plan_group(cur, user, GroupPurpose.PROJECT, PrivilegeLevel.CONTRIB, code, classification_code,
                        sponsor["user_id"], description=f"Contributors to project {name}",
                        project_id=project["project_id"])
        audit.record(cur, user, "PROJECT_REGISTERED", "PROJECT", project["project_id"], after=project)
        return project


@_service
def link_project_data_product(user: CurrentUser, project_id, data_product_id, justification: str) -> None:
    if len((justification or "").strip()) < 10:
        raise ServiceError("Explain why the project needs this data product.")
    with transaction() as cur:
        dp_owners = {r["email"] for r in fetch_rows(cur,
                     "SELECT u.email FROM ownership o JOIN app_user u ON u.user_id = o.user_id "
                     "WHERE o.object_kind = 'DATA_PRODUCT' AND o.object_id = %s AND o.role = 'DATA_OWNER' "
                     "AND o.valid_to IS NULL", (data_product_id,))}
        if not (user.has(AppRole.ADMIN) or user.email in dp_owners):
            raise ServiceError("Only the data product's owner or an admin can link it to a project.")
        cur.execute("INSERT INTO project_data_product (project_id, data_product_id, justification, linked_by) "
                    "VALUES (%s, %s, %s, %s) RETURNING *", (project_id, data_product_id, justification.strip(),
                                                           user.email))
        audit.record(cur, user, "PROJECT_DATA_PRODUCT_LINKED", "PROJECT", project_id, after=cur.fetchone())


def fetch_rows(cur, sql: str, params: Any = None) -> list[dict]:
    cur.execute(sql, params)
    return cur.fetchall()


@_service
def register_asset(user: CurrentUser, *, asset_type: str, name: str, external_id: str, workspace_id=None,
                   data_product_id=None, project_id=None, classification_code: str = "GENERAL") -> dict:
    _require(user, AppRole.ADMIN)
    if asset_type not in templates.ASSET_PRIVILEGES:
        raise ServiceError(f"Unknown asset type {asset_type}.")
    with transaction() as cur:
        cur.execute(
            "INSERT INTO asset (asset_type, name, external_id, workspace_id, data_product_id, project_id, "
            "classification_code, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (asset_type, name.strip(), external_id.strip(), workspace_id, data_product_id, project_id,
             classification_code, user.email),
        )
        row = cur.fetchone()
        audit.record(cur, user, "ASSET_REGISTERED", "ASSET", row["asset_id"], after=row)
        return row


@_service
def register_ai_use_case(user: CurrentUser, *, code: str, name: str, description: str | None,
                         data_product_id, asset_ids: list, owner_email: str,
                         include_data_product_read: bool = True) -> dict:
    """Creates the use case and its two Entra groups (-user, -admin) with default entitlements."""
    _require(user, AppRole.ADMIN)
    naming.validate_code(code)
    with transaction() as cur:
        dp = _must(_one(cur, "SELECT * FROM data_product WHERE data_product_id = %s", (data_product_id,)),
                   "Data product")
        cur.execute(
            "INSERT INTO ai_use_case (code, name, description, data_product_id, classification_code, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (code, name, description, data_product_id, dp["classification_code"], user.email),
        )
        uc = cur.fetchone()
        _assign_ownership(cur, user, "AI_USE_CASE", uc["ai_use_case_id"], owner_email, "DATA_OWNER")
        owner = ensure_user(cur, owner_email)
        assets = fetch_rows(cur, "SELECT asset_id, asset_type FROM asset WHERE asset_id = ANY(%s)", (list(asset_ids),))
        cur.execute("UPDATE asset SET ai_use_case_id = %s WHERE asset_id = ANY(%s) AND ai_use_case_id IS NULL",
                    (uc["ai_use_case_id"], list(asset_ids)))
        cat = _upsert_securable(cur, "CATALOG", dp["catalog_name"])
        sch = _upsert_securable(cur, "SCHEMA", f"{dp['catalog_name']}.{dp['schema_name']}",
                                dp["data_product_id"], dp["classification_code"])
        for level in (PrivilegeLevel.USER, PrivilegeLevel.ADMIN):
            group = _plan_group(cur, user, GroupPurpose.AI_USE_CASE, level, code, dp["classification_code"],
                                owner["user_id"], description=f"{level.value.title()}s of AI use case {name}",
                                ai_use_case_id=uc["ai_use_case_id"])
            for asset_id, priv in templates.ai_use_case_entitlements(
                    level, [(a["asset_id"], a["asset_type"]) for a in assets]):
                _add_entitlement_row(cur, user, group["access_group_id"], priv, asset_id=asset_id)
            if include_data_product_read:
                for kind, priv in templates.data_product_entitlements(PrivilegeLevel.READ):
                    target = cat if kind == "CATALOG" else sch
                    _add_entitlement_row(cur, user, group["access_group_id"], priv, securable_id=target["securable_id"])
        audit.record(cur, user, "AI_USE_CASE_REGISTERED", "AI_USE_CASE", uc["ai_use_case_id"], after=uc)
        return uc


@_service
def add_entitlement(user: CurrentUser, access_group_id, privilege: str, *, securable_type: str | None = None,
                    securable_full_name: str | None = None, asset_id=None) -> dict:
    _require(user, AppRole.ADMIN)
    with transaction() as cur:
        group = get_group(cur, access_group_id)
        if asset_id:
            asset = _must(_one(cur, "SELECT * FROM asset WHERE asset_id = %s", (asset_id,)), "Asset")
            templates.validate_asset_privilege(asset["asset_type"], privilege, group["privilege_level"])
            return _add_entitlement_row(cur, user, access_group_id, privilege, asset_id=asset_id)
        if not (securable_type and securable_full_name):
            raise ServiceError("Choose an asset or a Unity Catalog securable.")
        priv = ucsql.validate_privilege(securable_type, privilege)
        if priv not in ("USE CATALOG", "USE SCHEMA", "BROWSE", "SELECT", "EXECUTE", "READ VOLUME") and \
                group["privilege_level"] in ("READ", "USER"):
            raise ServiceError(f"{priv} is a write privilege; {group['privilege_level']} groups cannot hold it.")
        dp_id = group["effective_data_product_id"] if securable_type != "CATALOG" else None
        sec = _upsert_securable(cur, securable_type, securable_full_name, dp_id, group["classification_code"])
        return _add_entitlement_row(cur, user, access_group_id, priv, securable_id=sec["securable_id"])


@_service
def remove_entitlement(user: CurrentUser, entitlement_id) -> None:
    _require(user, AppRole.ADMIN)
    with transaction() as cur:
        before = _must(_one(cur, "SELECT * FROM group_entitlement WHERE entitlement_id = %s FOR UPDATE",
                            (entitlement_id,)), "Entitlement")
        cur.execute("UPDATE group_entitlement SET desired_state = 'ABSENT', apply_status = 'PENDING' "
                    "WHERE entitlement_id = %s RETURNING *", (entitlement_id,))
        audit.record(cur, user, "ENTITLEMENT_REMOVED", "GROUP_ENTITLEMENT", entitlement_id,
                     before=before, after=cur.fetchone())


@_service
def set_group_object_id(user: CurrentUser, access_group_id, entra_object_id: str) -> None:
    """For groups the identity team creates by hand: record the Entra object id and activate."""
    _require(user, AppRole.ADMIN)
    with transaction() as cur:
        before = get_group(cur, access_group_id)
        if before["status"] != "PLANNED":
            raise ServiceError("Only planned groups can be linked to an Entra object id.")
        cur.execute("UPDATE access_group SET entra_object_id = %s, status = 'ACTIVE' WHERE access_group_id = %s "
                    "RETURNING *", (entra_object_id.strip(), access_group_id))
        audit.record(cur, user, "GROUP_ACTIVATED", "ACCESS_GROUP", access_group_id, before=before, after=cur.fetchone())


@_service
def retire_group(user: CurrentUser, access_group_id, reason: str) -> None:
    _require(user, AppRole.ADMIN)
    if len((reason or "").strip()) < 10:
        raise ServiceError("Give a reason for retiring the group.")
    with transaction() as cur:
        before = get_group(cur, access_group_id)
        cur.execute("UPDATE access_group SET status = 'RETIRING', requestable = false WHERE access_group_id = %s",
                    (access_group_id,))
        for grant in fetch_rows(cur, "SELECT * FROM access_grant WHERE access_group_id = %s AND status = 'ACTIVE' "
                                     "FOR UPDATE", (access_group_id,)):
            mark_for_revoke(cur, grant, RevokeReason.GROUP_RETIRED, user, reason)
        cur.execute("UPDATE approval_task SET status = 'CANCELLED' WHERE status = 'PENDING' AND request_id IN "
                    "(SELECT request_id FROM access_request WHERE access_group_id = %s AND status = 'PENDING_APPROVAL')",
                    (access_group_id,))
        cur.execute("UPDATE access_request SET status = 'CANCELLED', decided_at = now() "
                    "WHERE access_group_id = %s AND status IN ('DRAFT', 'PENDING_APPROVAL')", (access_group_id,))
        cur.execute("UPDATE group_entitlement SET desired_state = 'ABSENT', apply_status = 'PENDING' "
                    "WHERE access_group_id = %s AND desired_state = 'PRESENT'", (access_group_id,))
        audit.record(cur, user, "GROUP_RETIRING", "ACCESS_GROUP", access_group_id, before=before, reason=reason)


@_service
def assign_ownership(user: CurrentUser, object_kind: str, object_id, email: str, role: str) -> None:
    _require(user, AppRole.ADMIN)
    with transaction() as cur:
        _assign_ownership(cur, user, object_kind, object_id, email, role)


@_service
def end_ownership(user: CurrentUser, ownership_id) -> None:
    _require(user, AppRole.ADMIN)
    with transaction() as cur:
        before = _must(_one(cur, "SELECT * FROM ownership WHERE ownership_id = %s FOR UPDATE", (ownership_id,)),
                       "Ownership")
        cur.execute("UPDATE ownership SET valid_to = now() WHERE ownership_id = %s RETURNING *", (ownership_id,))
        audit.record(cur, user, "OWNERSHIP_ENDED", before["object_kind"], before["object_id"],
                     before=before, after=cur.fetchone())


@_service
def add_delegation(user: CurrentUser, delegator_email: str, delegate_email: str, valid_to: datetime,
                   reason: str) -> None:
    delegator_email = delegator_email.strip().lower()
    if delegator_email != user.email:
        _require(user, AppRole.ADMIN)
    if len((reason or "").strip()) < 5:
        raise ServiceError("Give a reason for the delegation (for example annual leave).")
    with transaction() as cur:
        a = ensure_user(cur, delegator_email)
        b = ensure_user(cur, delegate_email)
        cur.execute("INSERT INTO delegation (delegator_user_id, delegate_user_id, valid_to, reason, created_by) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING *", (a["user_id"], b["user_id"], valid_to, reason, user.email))
        row = cur.fetchone()
        audit.record(cur, user, "DELEGATION_CREATED", "DELEGATION", row["delegation_id"], after=row)


@_service
def set_app_role(user: CurrentUser, email: str, role: AppRole, grant: bool) -> None:
    _require(user, AppRole.ADMIN)
    email = email.strip().lower()
    if email == user.email:
        raise ServiceError("You cannot change your own app roles.")
    with transaction() as cur:
        if grant:
            cur.execute("INSERT INTO app_role_member (email, app_role, granted_by) VALUES (%s, %s, %s) "
                        "ON CONFLICT DO NOTHING", (email, AppRole(role).value, user.email))
        else:
            cur.execute("DELETE FROM app_role_member WHERE email = %s AND app_role = %s", (email, AppRole(role).value))
        audit.record(cur, user, "APP_ROLE_GRANTED" if grant else "APP_ROLE_REMOVED", "APP_ROLE", email,
                     after={"email": email, "role": AppRole(role).value})


@_service
def retry_action(user: CurrentUser, action_id) -> None:
    _require(user, AppRole.ADMIN)
    with transaction() as cur:
        action = _must(_one(cur, "SELECT * FROM provisioning_action WHERE action_id = %s FOR UPDATE", (action_id,)),
                       "Action")
        if action["status"] != "DEAD":
            raise ServiceError("Only dead-lettered actions can be retried.")
        cur.execute("UPDATE provisioning_action SET status = 'QUEUED', attempts = 0, next_attempt_at = now(), "
                    "last_error = NULL WHERE action_id = %s", (action_id,))
        if action["grant_id"]:
            grant = _one(cur, "SELECT * FROM access_grant WHERE grant_id = %s FOR UPDATE", (action["grant_id"],))
            if grant["status"] == GrantStatus.FAILED.value:
                cur.execute("UPDATE access_grant SET status = %s WHERE grant_id = %s",
                            (workflow.next_grant_status(grant["status"], "retry").value, grant["grant_id"]))
                req = _one(cur, "SELECT * FROM access_request WHERE request_id = %s FOR UPDATE", (grant["request_id"],))
                if req["status"] == RequestStatus.PROVISIONING_FAILED.value:
                    _set_request_status(cur, req["request_id"],
                                        workflow.next_request_status(req["status"], "retry", "admin"))
        if action["entitlement_id"]:
            cur.execute("UPDATE group_entitlement SET apply_status = 'PENDING', last_error = NULL "
                        "WHERE entitlement_id = %s AND apply_status = 'FAILED'", (action["entitlement_id"],))
        audit.record(cur, user, "ACTION_RETRIED", "PROVISIONING_ACTION", action_id, before=action)


@_service
def update_drift_finding(user: CurrentUser, finding_id, status: str, note: str) -> None:
    _require(user, AppRole.SECURITY, AppRole.ADMIN)
    if status not in ("ACKNOWLEDGED", "ACCEPTED_RISK", "REMEDIATED"):
        raise ServiceError("Invalid status.")
    if len((note or "").strip()) < 10:
        raise ServiceError("Add a note of at least 10 characters.")
    if status == "ACCEPTED_RISK" and not user.has(AppRole.SECURITY):
        raise ServiceError("Only security can accept a risk.")
    with transaction() as cur:
        before = _must(_one(cur, "SELECT * FROM drift_finding WHERE finding_id = %s FOR UPDATE", (finding_id,)),
                       "Finding")
        closing = status == "REMEDIATED"
        cur.execute("UPDATE drift_finding SET status = %s, resolution_note = %s, resolved_by = %s, "
                    "resolved_at = CASE WHEN %s THEN now() ELSE resolved_at END WHERE finding_id = %s RETURNING *",
                    (status, note.strip(), user.email, closing, finding_id))
        audit.record(cur, user, "DRIFT_FINDING_UPDATED", "DRIFT_FINDING", finding_id, before=before,
                     after=cur.fetchone(), reason=note)


def verify_audit_chain() -> list[dict]:
    return fetch_all("SELECT * FROM verify_audit_chain()")
