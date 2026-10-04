"""Business operations. Every state change the apps make happens here, with
authorisation, segregation-of-duties checks and an audit record.

Delta has no multi-table transactions, so each operation is a short sequence of
single-statement writes. Each write that depends on current state checks it in
the same statement (UPDATE ... WHERE status = :expected, INSERT ... WHERE NOT
EXISTS) and the affected row count is checked, so two people acting at once
cannot both succeed. Validation happens before the first write; the audit event
is written straight after the change it records.

The app records and approves access but never changes a group: an admin adds or
removes the member by hand and marks the grant done (`mark_granted`,
`mark_removed`), so every manual change is attributed and audited.

The App calls these as the signed-in user; jobs call the shared helpers
(`mark_for_revoke`, `owners_for_group`, ...) with a system actor.
"""
from __future__ import annotations

import functools
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from . import audit, db, naming, notify, workflow
from .config import get_settings
from .db import fetch_all, fetch_one
from .models import AppRole, ApproverRole, CurrentUser, GrantStatus, RequestStatus, RequestType, RevokeReason
from .workflow import ApprovalContext, ApprovalRule

log = logging.getLogger(__name__)

LIVE_GRANT_STATES = ("PENDING_PROVISION", "ACTIVE", "PENDING_REVOKE")
OPEN_GRANT_REQUEST_STATES = ("DRAFT", "PENDING_APPROVAL", "APPROVED")
CLASSIFICATIONS = ("GENERAL", "CONFIDENTIAL", "RESTRICTED")
WORKSPACE_TYPES = {
    "INGESTION": "Ingestion",
    "ANALYTICS_PBI": "Analytics (Power BI)",
    "MLOPS": "MLOps",
    "DATABRICKS_DASHBOARDS": "Databricks Dashboards",
}
DATA_PRODUCT_TYPES = {
    "DATABRICKS_DASHBOARD": "Databricks AI/BI dashboard",
    "POWER_BI": "Power BI",
    "GENIE_AGENT": "Genie agent",
    "CUSTOM_AGENT": "Custom agent",
    "DATABRICKS_APP": "Databricks app",
    "SCHEMA": "Schema",
}
ENVIRONMENTS = ("dev", "tst", "prd")
STALE = "This changed while you were working on it. Refresh the page and try again."


class ServiceError(Exception):
    """A user-facing, expected failure (validation, authorisation, state)."""


def _service(fn: Callable) -> Callable:
    """Translate domain and warehouse errors into ServiceError so the UI can show them plainly."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ServiceError:
            raise
        except (workflow.TransitionError, workflow.ConfigurationError, naming.NamingError) as exc:
            raise ServiceError(str(exc)) from exc
        except Exception as exc:
            text = str(exc)
            if "INSUFFICIENT_PERMISSIONS" in text or "PERMISSION_DENIED" in text:
                log.warning("Warehouse refused %s for this app: %s", fn.__name__, text[:300])
                raise ServiceError("This app is not permitted to make that change. "
                                   "Use the Data access app or the Catalog admin app as appropriate.") from exc
            match = re.search(r"CHECK constraint (\w+)", text)
            if match:
                raise ServiceError(f"The data failed a validation rule: {match.group(1)}") from exc
            raise
    return wrapper


def _require(user: CurrentUser, *roles: AppRole) -> None:
    if not any(user.has(r) for r in roles):
        names = " or ".join(r.value.lower().replace("_", " ") for r in roles)
        raise ServiceError(f"You need the {names} role to do this.")


def _must(row: dict | None, what: str) -> dict:
    if row is None:
        raise ServiceError(f"{what} was not found.")
    return row


def _email(value: str | None, what: str = "Email") -> str:
    email = (value or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ServiceError(f"{what} {value!r} is not a valid email address.")
    return email


def _next_code(prefix: str, width: int) -> str:
    """SQL for a generated identifier from subquery `s.seq`: WS-0001, ..., WS-10000 (never truncated)."""
    seq = "CAST(s.seq AS STRING)"
    return f"concat('{prefix}-', lpad({seq}, greatest({width}, length({seq})), '0'))"


# ---------------------------------------------------------------------------
# Shared lookups (also used by jobs)
# ---------------------------------------------------------------------------
def ensure_user(email: str, display_name: str | None = None) -> dict:
    email = _email(email)
    db.execute(
        """
        MERGE INTO app_user t
        USING (SELECT :email AS email, CAST(:name AS STRING) AS display_name) s ON t.email = s.email
        WHEN MATCHED AND s.display_name IS NOT NULL AND t.display_name IS DISTINCT FROM s.display_name THEN
          UPDATE SET display_name = s.display_name, updated_at = current_timestamp()
        WHEN NOT MATCHED THEN
          INSERT (email, display_name, is_active, last_seen_at, created_at, updated_at)
          VALUES (s.email, s.display_name, true, current_timestamp(), current_timestamp(), current_timestamp())
        """, {"email": email, "name": display_name})
    return fetch_one("SELECT * FROM app_user WHERE email = :email", {"email": email})


def load_rules() -> list[ApprovalRule]:
    return [ApprovalRule(r["stage_no"], r["classification_code"], ApproverRole(r["approver_role"]), r["sla_hours"])
            for r in fetch_all("SELECT * FROM approval_rule ORDER BY classification_code, stage_no")]


def classification(code: str) -> dict:
    return _must(fetch_one("SELECT * FROM classification WHERE classification_code = :c", {"c": code}),
                 "Classification")


def get_group(group_code: str) -> dict:
    return _must(fetch_one("SELECT * FROM v_group WHERE group_code = :g", {"g": group_code}), "Access group")


def _grant(grant_id: str) -> dict:
    return _must(fetch_one("SELECT * FROM access_grant WHERE grant_id = :id", {"id": grant_id}), "Grant")


def owners_for_group(group: dict) -> dict[ApproverRole, set[str]]:
    """Data owner and steward of the group's data product (FR-29), if still active people."""
    named = {ApproverRole.DATA_OWNER: group.get("owner_email"), ApproverRole.STEWARD: group.get("steward_email")}
    emails = [e for e in named.values() if e]
    inactive = {r["email"] for r in fetch_all("SELECT email FROM app_user WHERE email IN (:e) AND NOT is_active",
                                              {"e": emails})}
    return {role: {email} for role, email in named.items() if email and email not in inactive}


def active_delegations() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in fetch_all(
            "SELECT d.delegator_email, d.delegate_email FROM delegation d "
            "LEFT JOIN app_user u ON u.email = d.delegate_email "
            "WHERE current_timestamp() BETWEEN d.valid_from AND d.valid_to AND coalesce(u.is_active, true)"):
        out.setdefault(r["delegator_email"], set()).add(r["delegate_email"])
    return out


def _prior_approvers(request_ids: list) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in fetch_all("SELECT request_id, decided_by_email FROM approval_task WHERE request_id IN (:ids) "
                       "AND status = 'APPROVED' AND NOT is_post_hoc", {"ids": list(request_ids)}):
        out.setdefault(r["request_id"], set()).add(r["decided_by_email"])
    return out


def approval_context(request: dict, group: dict, delegations: dict[str, set[str]] | None = None,
                     prior: set[str] | None = None) -> ApprovalContext:
    """request is a v_request (or v_task) row."""
    if prior is None:
        prior = _prior_approvers([request["request_id"]]).get(request["request_id"], set())
    return ApprovalContext(
        requester=request["requester_email"], beneficiary=request["beneficiary_email"],
        role_holders=owners_for_group(group),
        delegations=active_delegations() if delegations is None else delegations,
        prior_approvers=set(prior),
    )


def _create_task(request_id: str, rule: ApprovalRule, now: datetime, *, is_post_hoc: bool = False,
                 due_at: datetime | None = None) -> dict:
    settings = get_settings()
    task = {"task_id": str(uuid.uuid4()), "request_id": request_id, "stage_no": rule.stage_no,
            "approver_role": rule.approver_role.value, "is_post_hoc": is_post_hoc, "now": now,
            "due_at": due_at or workflow.add_business_hours(now, rule.sla_hours, settings.holidays, settings.timezone)}
    n = db.execute(
        """
        INSERT INTO approval_task (task_id, request_id, stage_no, approver_role, status, is_post_hoc, due_at, created_at)
        SELECT :task_id, :request_id, :stage_no, :approver_role, 'PENDING', :is_post_hoc, :due_at, :now
        FROM (SELECT 1) one
        WHERE NOT EXISTS (SELECT 1 FROM approval_task WHERE request_id = :request_id AND stage_no = :stage_no)
        """, task)
    if n != 1:
        raise ServiceError(STALE)
    return task


def access_label(row: dict) -> str:
    """What a group gives, e.g. "Member contributions (Power BI)". row is a v_group / v_request / v_grant row."""
    kind = DATA_PRODUCT_TYPES.get(row.get("data_product_type"), row.get("data_product_type") or "")
    return f"{row['data_product_name']} ({kind})" if kind else row["data_product_name"]


def _describe(req: dict) -> str:
    return "\n".join([
        f"Reference: {req['request_ref']}",
        f"Access: {access_label(req)} - group {req['group_name']}",
        f"Classification: {req['classification_code']}",
        f"Requested by: {req['requester_email']}",
        f"For: {req['beneficiary_email']}",
        f"Justification: {req['justification']}",
    ])


def _notify_stage(req: dict, rule: ApprovalRule, ctx: ApprovalContext, due: datetime) -> None:
    subject = f"Approval needed: {req['request_ref']} ({req['classification_code']})"
    body = _describe(req) + f"\n\nStage {rule.stage_no}: {rule.approver_role.value}. Due {due:%d %b %Y %H:%M} UTC."
    if rule.approver_role == ApproverRole.SECURITY:
        notify.security_alert(subject, body, "APPROVAL_NEEDED", request_id=req["request_id"],
                              dedupe_key=f"approval:{req['request_id']}:{rule.stage_no}")
        return
    recipients = workflow.eligible_approvers(rule.approver_role, ctx)
    if not recipients:
        notify.admin_alert(f"No eligible approver for {req['request_ref']}",
                           body + "\n\nSet the data product's owner / steward, or a delegate, so the request "
                                  "can proceed.", "NO_APPROVER",
                           dedupe_key=f"noapprover:{req['request_id']}:{rule.stage_no}")
        return
    notify.enqueue(recipients.keys(), subject, body, "APPROVAL_NEEDED", request_id=req["request_id"],
                   dedupe_key=f"approval:{req['request_id']}:{rule.stage_no}")


def _v_request(request_id: str) -> dict:
    return _must(fetch_one("SELECT * FROM v_request WHERE request_id = :id", {"id": request_id}), "Request")


def set_request_status(request_id: str, expected: RequestStatus | str, status: RequestStatus, **extra) -> None:
    """Move a request on, only if it is still in the expected state."""
    sets = ["status = :status", "version = version + 1", "updated_at = current_timestamp()"]
    sets += [f"{col} = :{col}" for col in extra]
    n = db.execute(f"UPDATE access_request SET {', '.join(sets)} WHERE request_id = :id AND status = :expected",
                   {"status": status, "id": request_id, "expected": str(expected), **extra})
    if n != 1:
        raise ServiceError(STALE)


def mark_for_revoke(grant: dict, reason: RevokeReason, actor: CurrentUser | str,
                    note: str | None = None) -> dict | None:
    """ACTIVE -> PENDING_REVOKE. An admin removes the member by hand and marks it removed.
    Returns None if someone else changed the grant first."""
    new = workflow.next_grant_status(grant["status"], "revoke")
    actor_name = actor.email if isinstance(actor, CurrentUser) else str(actor)
    n = db.execute(
        "UPDATE access_grant SET status = :new, revoke_reason = :reason, revoke_requested_at = current_timestamp(), "
        "revoked_by = :by, updated_at = current_timestamp() WHERE grant_id = :id AND status = :current",
        {"new": new, "reason": reason, "by": actor_name, "id": grant["grant_id"], "current": grant["status"]})
    if n != 1:
        return None
    after = fetch_one("SELECT * FROM v_grant WHERE grant_id = :id", {"id": grant["grant_id"]})
    audit.record(actor, "GRANT_REVOKE_REQUESTED", "ACCESS_GRANT", grant["grant_id"],
                 before=grant, after=after, reason=note or reason.value, correlation_id=after["request_ref"])
    notify.enqueue([after["user_email"]], f"Access is being removed: {after['group_name']}",
                   f"Your access to {access_label(after)} (group {after['group_name']}) is being "
                   f"removed. Reason: {reason.value}." + (f"\nNote: {note}" if note else ""), "ACCESS_REVOKING",
                   request_id=grant["request_id"], dedupe_key=f"revoking:{grant['grant_id']}")
    notify.admin_alert(f"Remove member: {after['user_email']} from {after['group_name']}",
                       f"Remove {after['user_email']} from group {after['group_name']} ({access_label(after)}), "
                       f"then mark it removed on the Fulfilment page. Reason: {reason.value}.", "REMOVE_MEMBER",
                       dedupe_key=f"remove:{grant['grant_id']}")
    return after


def withdraw_pending_grant(grant: dict, reason: RevokeReason, actor: CurrentUser | str, note: str) -> bool:
    """PENDING_PROVISION -> REVOKED: approved access that was never added (group retired, person left).
    Its request is closed as cancelled. Returns False if an admin marked it granted first."""
    actor_name = actor.email if isinstance(actor, CurrentUser) else str(actor)
    kind = "admin" if isinstance(actor, CurrentUser) and actor.actor_type == "USER" else "system"
    new = workflow.next_grant_status(grant["status"], "withdraw")
    n = db.execute(
        "UPDATE access_grant SET status = :new, revoke_reason = :reason, revoke_requested_at = current_timestamp(), "
        "revoked_at = current_timestamp(), revoked_by = :by, updated_at = current_timestamp() "
        "WHERE grant_id = :id AND status = 'PENDING_PROVISION'",
        {"new": new, "reason": reason, "by": actor_name, "id": grant["grant_id"]})
    if n != 1:
        return False
    req = fetch_one("SELECT status FROM access_request WHERE request_id = :id", {"id": grant["request_id"]})
    if req and req["status"] == RequestStatus.APPROVED.value:
        db.execute("UPDATE access_request SET status = :s, version = version + 1, updated_at = current_timestamp() "
                   "WHERE request_id = :id AND status = 'APPROVED'",
                   {"s": workflow.next_request_status(req["status"], "withdraw", kind), "id": grant["request_id"]})
    audit.record(actor, "GRANT_WITHDRAWN", "ACCESS_GRANT", grant["grant_id"], before=grant, reason=note)
    return True


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def _insert_request(request_id: str, values: dict, duplicate_condition: str, duplicate_params: dict) -> bool:
    """Insert a request with the next SAR reference, unless the duplicate condition matches. Returns inserted?"""
    n = db.execute(
        f"""
        INSERT INTO access_request (request_id, request_seq, request_ref, request_type, requester_email,
            beneficiary_email, group_code, extends_grant_id, classification_code, justification, incident_ref,
            requested_duration_days, status, current_stage_no, submitted_at, decided_at, version, created_at,
            updated_at)
        SELECT :id, s.seq, {_next_code('SAR', 7)}, :type, :requester, :beneficiary, :group_code,
               CAST(:extends AS STRING), :cls, :why, CAST(:incident AS STRING), CAST(:days AS INT), :status,
               CAST(:stage AS INT), :now, CAST(:decided AS TIMESTAMP), 1, :now, :now
        FROM (SELECT coalesce(max(request_seq), 0) + 1 AS seq FROM access_request) s
        WHERE NOT EXISTS ({duplicate_condition})
        """, {"id": request_id, **values, **duplicate_params})
    return n == 1


@_service
def submit_request(user: CurrentUser, *, group_code: str, justification: str, duration_days: int,
                   beneficiary_email: str | None = None, request_type: RequestType = RequestType.GRANT,
                   extends_grant_id: str | None = None) -> dict:
    justification = (justification or "").strip()
    if len(justification) < 20:
        raise ServiceError("Give a business justification of at least 20 characters.")
    rtype = RequestType(request_type)
    if rtype == RequestType.BREAK_GLASS:
        raise ServiceError("Use the break-glass page for emergency access.")
    requester = ensure_user(user.email, user.display_name)
    beneficiary = ensure_user(beneficiary_email or user.email)
    if not beneficiary["is_active"]:
        raise ServiceError(f"{beneficiary['email']} is not an active user.")
    group = get_group(group_code)
    if group["status"] != "ACTIVE" or group["data_product_status"] != "ACTIVE":
        raise ServiceError(f"{group['group_name']} is not open for requests.")

    if rtype == RequestType.EXTEND:
        grant = _grant(extends_grant_id)
        if (grant["status"] != GrantStatus.ACTIVE.value or grant["user_email"] != beneficiary["email"]
                or grant["group_code"] != group_code):
            raise ServiceError("Only your own active access can be extended.")
        if grant["is_break_glass"]:
            raise ServiceError("Break-glass access cannot be extended; raise a normal request.")
        duplicate = ("SELECT 1 FROM access_request r WHERE r.request_type = 'EXTEND' "
                     "AND r.extends_grant_id = :dup_grant AND r.status IN ('DRAFT', 'PENDING_APPROVAL')")
        dup_params: dict[str, Any] = {"dup_grant": extends_grant_id}
    else:
        if not group["requestable"]:
            raise ServiceError(f"{group['group_name']} is not open for self-service requests.")
        if fetch_one("SELECT 1 AS x FROM access_grant WHERE user_email = :e AND group_code = :g AND status IN (:live)",
                     {"e": beneficiary["email"], "g": group_code, "live": list(LIVE_GRANT_STATES)}):
            raise ServiceError("This person already has (or is being given) this access. "
                               "Use Extend on the My access page instead.")
        duplicate = ("SELECT 1 FROM access_request r WHERE r.request_type = 'GRANT' AND r.beneficiary_email = "
                     ":dup_email AND r.group_code = :dup_group AND r.status IN (:dup_open)")
        dup_params = {"dup_email": beneficiary["email"], "dup_group": group_code,
                      "dup_open": list(OPEN_GRANT_REQUEST_STATES)}

    stages = workflow.plan_stages(load_rules(), group["classification_code"], rtype)
    if not duration_days or int(duration_days) < 1:
        raise ServiceError("Choose an end date after today.")
    duration = int(duration_days)
    request_id, now = str(uuid.uuid4()), db.now()
    status = workflow.next_request_status(RequestStatus.DRAFT, "submit", "requester")
    inserted = _insert_request(request_id, {
        "type": rtype, "requester": requester["email"], "beneficiary": beneficiary["email"], "group_code": group_code,
        "extends": extends_grant_id, "cls": group["classification_code"], "why": justification, "incident": None,
        "days": duration, "status": status, "stage": 1, "now": now, "decided": None}, duplicate, dup_params)
    if not inserted:
        raise ServiceError("There is already an open request for this access. Follow it on My access.")
    task = _create_task(request_id, stages[0], now)
    req = _v_request(request_id)
    audit.record(user, "REQUEST_SUBMITTED", "ACCESS_REQUEST", request_id, after=req, correlation_id=req["request_ref"])
    _notify_stage(req, stages[0], approval_context(req, group, prior=set()), task["due_at"])
    return req


@_service
def request_extension(user: CurrentUser, grant_id: str, duration_days: int, justification: str) -> dict:
    grant = _must(fetch_one("SELECT * FROM v_grant WHERE grant_id = :id", {"id": grant_id}), "Grant")
    if grant["user_email"] != user.email:
        raise ServiceError("You can only extend your own access.")
    return submit_request(user, group_code=grant["group_code"], justification=justification,
                          duration_days=duration_days, request_type=RequestType.EXTEND, extends_grant_id=grant_id)


@_service
def decide_task(user: CurrentUser, task_id: str, approve: bool, comment: str | None = None) -> dict:
    comment = (comment or "").strip()
    if not approve and len(comment) < 5:
        raise ServiceError("Say why you are rejecting (at least 5 characters).")
    task = _must(fetch_one("SELECT * FROM approval_task WHERE task_id = :id", {"id": task_id}), "Task")
    if task["status"] != "PENDING":
        raise ServiceError("This approval has already been decided.")
    raw = _must(fetch_one("SELECT * FROM access_request WHERE request_id = :id", {"id": task["request_id"]}),
                "Request")
    req = _v_request(raw["request_id"])
    group = get_group(raw["group_code"])
    ctx = approval_context(req, group)
    ok, reason, on_behalf = workflow.can_decide(user.email, user.app_roles, task["approver_role"], ctx)
    if not ok:
        raise ServiceError(reason)
    ensure_user(user.email, user.display_name)
    now = db.now()
    decision = "APPROVED" if approve else "REJECTED"
    n = db.execute(
        "UPDATE approval_task SET status = :decision, decided_by_email = :by, on_behalf_of_email = :obo, "
        "decided_at = :now, decision_comment = :comment WHERE task_id = :id AND status = 'PENDING'",
        {"decision": decision, "by": user.email, "obo": on_behalf, "now": now, "comment": comment or None,
         "id": task_id})
    if n != 1:
        raise ServiceError("This approval has already been decided.")
    decided = {**task, "status": decision, "decided_by_email": user.email, "on_behalf_of_email": on_behalf,
               "decided_at": now, "decision_comment": comment or None}
    audit.record(user, f"TASK_{decision}", "APPROVAL_TASK", task_id, before=task, after=decided,
                 reason=comment or None, correlation_id=req["request_ref"])

    if task["is_post_hoc"]:  # break-glass review
        if not approve:
            grant = fetch_one("SELECT * FROM access_grant WHERE request_id = :id", {"id": raw["request_id"]})
            if grant and grant["status"] == GrantStatus.ACTIVE.value:
                mark_for_revoke(grant, RevokeReason.POST_HOC_REJECTED, user, comment)
            notify.security_alert(f"Break-glass use rejected on review: {req['request_ref']}",
                                  _describe(req) + f"\n\nReviewer: {user.email}\nComment: {comment}",
                                  "BREAK_GLASS_REJECTED", request_id=raw["request_id"])
        return decided

    if not approve:
        status = workflow.next_request_status(raw["status"], "reject", "approver")
        set_request_status(raw["request_id"], raw["status"], status, decided_at=now)
        audit.record(user, "REQUEST_REJECTED", "ACCESS_REQUEST", raw["request_id"], reason=comment,
                     correlation_id=req["request_ref"])
        notify.enqueue([req["requester_email"], req["beneficiary_email"]], f"Request rejected: {req['request_ref']}",
                       _describe(req) + f"\n\nRejected by {user.email}: {comment}", "REQUEST_REJECTED",
                       request_id=raw["request_id"])
        return decided

    stages = workflow.plan_stages(load_rules(), raw["classification_code"], raw["request_type"])
    remaining = [s for s in stages if s.stage_no > task["stage_no"]]
    if remaining:
        status = workflow.next_request_status(raw["status"], "approve_stage", "approver")
        set_request_status(raw["request_id"], raw["status"], status, current_stage_no=remaining[0].stage_no)
        new_task = _create_task(raw["request_id"], remaining[0], now)
        ctx.prior_approvers.add(user.email)
        _notify_stage(req, remaining[0], ctx, new_task["due_at"])
    else:
        _finalise_approval(user, raw, req, now)
    return decided


def _cancel_by_system(user: CurrentUser, raw: dict, req: dict, now: datetime, why: str) -> None:
    status = workflow.next_request_status(raw["status"], "cancel", "system")
    set_request_status(raw["request_id"], raw["status"], status, decided_at=now)
    audit.record(user, "REQUEST_CANCELLED", "ACCESS_REQUEST", raw["request_id"], reason=why,
                 correlation_id=req["request_ref"])
    notify.enqueue([req["requester_email"], req["beneficiary_email"]], f"Request closed: {req['request_ref']}",
                   f"{why}. Nothing else needs to be done.", "REQUEST_CANCELLED", request_id=raw["request_id"])


def _finalise_approval(user: CurrentUser, raw: dict, req: dict, now: datetime) -> None:
    cls = classification(raw["classification_code"])
    # A new grant ends on the date the requester chose (counted from submission); an extension from now
    start = now if raw["request_type"] == RequestType.EXTEND.value else raw["submitted_at"]
    valid_to = max(workflow.effective_valid_to(start, raw["requested_duration_days"], cls["max_grant_days"]),
                   now + timedelta(days=1))

    if raw["request_type"] == RequestType.EXTEND.value:
        grant = fetch_one("SELECT * FROM access_grant WHERE grant_id = :id", {"id": raw["extends_grant_id"]})
        new_to = max(valid_to, grant["valid_to"]) if grant else valid_to
        n = db.execute("UPDATE access_grant SET valid_to = :to, expiry_notice_level = 0, last_recertified_at = :now, "
                       "updated_at = current_timestamp() WHERE grant_id = :id AND status = 'ACTIVE'",
                       {"to": new_to, "now": now, "id": raw["extends_grant_id"]})
        if n != 1:
            _cancel_by_system(user, raw, req, now, "The access being extended is no longer active")
            return
        status = workflow.next_request_status(raw["status"], "approve_final", "approver")
        status = workflow.next_request_status(status, "extended", "approver")
        set_request_status(raw["request_id"], raw["status"], status, decided_at=now, fulfilled_at=now)
        audit.record(user, "GRANT_EXTENDED", "ACCESS_GRANT", grant["grant_id"], before=grant,
                     after={**grant, "valid_to": new_to, "last_recertified_at": now}, correlation_id=req["request_ref"])
        notify.enqueue([req["beneficiary_email"]], f"Access extended: {req['group_name']}",
                       f"Your access now runs until {new_to:%d %b %Y}.", "ACCESS_EXTENDED",
                       request_id=raw["request_id"])
        return

    live = {"e": raw["beneficiary_email"], "g": raw["group_code"], "live": list(LIVE_GRANT_STATES)}
    if fetch_one("SELECT 1 AS x FROM access_grant WHERE user_email = :e AND group_code = :g AND status IN (:live)",
                 live):
        _cancel_by_system(user, raw, req, now, f"{raw['beneficiary_email']} already has this access")
        return
    status = workflow.next_request_status(raw["status"], "approve_final", "approver")
    set_request_status(raw["request_id"], raw["status"], status, decided_at=now)
    grant_id = str(uuid.uuid4())
    n = db.execute(
        """
        INSERT INTO access_grant (grant_id, request_id, user_email, group_code, status, is_break_glass, valid_from,
                                  valid_to, last_recertified_at, expiry_notice_level, created_at, updated_at)
        SELECT :id, :request_id, :e, :g, 'PENDING_PROVISION', false, :now, :to, :now, 0, :now, :now
        FROM (SELECT 1) one
        WHERE NOT EXISTS (SELECT 1 FROM access_grant WHERE user_email = :e AND group_code = :g AND status IN (:live))
        """, {"id": grant_id, "request_id": raw["request_id"], "now": now, "to": valid_to, **live})
    if n != 1:
        log.error("Request %s approved but a live grant appeared meanwhile", req["request_ref"])
        raise ServiceError("Approved, but this person was given the same access at the same moment. "
                           "Ask an admin to check the request.")
    grant = _grant(grant_id)
    audit.record(user, "REQUEST_APPROVED", "ACCESS_REQUEST", raw["request_id"], correlation_id=req["request_ref"])
    audit.record(user, "GRANT_CREATED", "ACCESS_GRANT", grant_id, after=grant, correlation_id=req["request_ref"])
    notify.enqueue([req["requester_email"], req["beneficiary_email"]], f"Request approved: {req['request_ref']}",
                   _describe(req) + "\n\nApproved. An admin will add you to the group; you will be told when it is "
                   "done.", "REQUEST_APPROVED", request_id=raw["request_id"])
    notify.admin_alert(f"Add member: {req['beneficiary_email']} to {req['group_name']}",
                       _describe(req) + "\n\nAdd the person to the group, then mark it granted on the "
                       "Fulfilment page.", "ADD_MEMBER", dedupe_key=f"add:{grant_id}")


@_service
def cancel_request(user: CurrentUser, request_id: str) -> None:
    raw = _must(fetch_one("SELECT * FROM access_request WHERE request_id = :id", {"id": request_id}), "Request")
    req = _v_request(request_id)
    if user.email in (req["requester_email"], req["beneficiary_email"]):
        actor_kind = "requester"
    elif user.has(AppRole.ADMIN):
        actor_kind = "admin"
    else:
        raise ServiceError("Only the requester, the person it is for, or an admin can cancel this request.")
    status = workflow.next_request_status(raw["status"], "cancel", actor_kind)
    set_request_status(request_id, raw["status"], status, decided_at=db.now())
    db.execute("UPDATE approval_task SET status = 'CANCELLED' WHERE request_id = :id AND status = 'PENDING'",
               {"id": request_id})
    audit.record(user, "REQUEST_CANCELLED", "ACCESS_REQUEST", request_id, before=raw, correlation_id=req["request_ref"])


@_service
def break_glass(user: CurrentUser, group_code: str, incident_ref: str, justification: str) -> dict:
    _require(user, AppRole.BREAK_GLASS)
    incident_ref = (incident_ref or "").strip()
    justification = (justification or "").strip()
    if not incident_ref or len(justification) < 20:
        raise ServiceError("An incident reference and a justification (20+ characters) are required.")
    settings = get_settings()
    me = ensure_user(user.email, user.display_name)
    group = get_group(group_code)
    if group["status"] != "ACTIVE":
        raise ServiceError("Break-glass needs an active group.")
    request_id, now = str(uuid.uuid4()), db.now()
    status = workflow.next_request_status(RequestStatus.DRAFT, "break_glass", "break_glass")
    live = {"dup_e": me["email"], "dup_g": group_code, "dup_live": list(LIVE_GRANT_STATES)}
    inserted = _insert_request(request_id, {
        "type": RequestType.BREAK_GLASS, "requester": me["email"], "beneficiary": me["email"],
        "group_code": group_code, "extends": None, "cls": group["classification_code"], "why": justification,
        "incident": incident_ref, "days": None, "status": status, "stage": None, "now": now, "decided": now},
        "SELECT 1 FROM access_grant WHERE user_email = :dup_e AND group_code = :dup_g AND status IN (:dup_live)",
        live)
    if not inserted:
        raise ServiceError("You already have this access.")
    valid_to = workflow.effective_valid_to(now, None, 1, request_type=RequestType.BREAK_GLASS,
                                           break_glass_hours=settings.break_glass_hours)
    grant_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO access_grant (grant_id, request_id, user_email, group_code, status, is_break_glass, valid_from, "
        "valid_to, expiry_notice_level, created_at, updated_at) "
        "VALUES (:id, :request_id, :e, :g, 'PENDING_PROVISION', true, :now, :to, 0, :now, :now)",
        {"id": grant_id, "request_id": request_id, "e": me["email"], "g": group_code, "now": now, "to": valid_to})
    review = ApprovalRule(1, group["classification_code"], ApproverRole.SECURITY, 1)
    _create_task(request_id, review, now, is_post_hoc=True,
                 due_at=now + timedelta(hours=settings.break_glass_review_hours))
    req = _v_request(request_id)
    audit.record(user, "BREAK_GLASS_INVOKED", "ACCESS_GRANT", grant_id, after=_grant(grant_id),
                 reason=f"{incident_ref}: {justification}", correlation_id=req["request_ref"])
    notify.security_alert(f"Break-glass access used: {group['group_name']}",
                          _describe(req) + f"\nIncident: {incident_ref}\nLasts {settings.break_glass_hours} hours "
                          f"once an admin adds the member.\nA security reviewer must review this within "
                          f"{settings.break_glass_review_hours} hours.", "BREAK_GLASS_INVOKED", request_id=request_id)
    notify.admin_alert(f"URGENT - add member: {me['email']} to {group['group_name']}",
                       _describe(req) + f"\nIncident: {incident_ref}\n\nEmergency access: add the person to the "
                       "group now, then mark it granted on the Fulfilment page.", "ADD_MEMBER",
                       dedupe_key=f"add:{grant_id}")
    return req


@_service
def revoke_grant(user: CurrentUser, grant_id: str, note: str | None = None) -> dict:
    grant = _grant(grant_id)
    if grant["status"] != GrantStatus.ACTIVE.value:
        raise ServiceError("Only active access can be removed.")
    group = get_group(grant["group_code"])
    if user.email == grant["user_email"]:
        reason = RevokeReason.SELF
    elif user.has(AppRole.ADMIN):
        reason = RevokeReason.ADMIN
    elif user.email in owners_for_group(group).get(ApproverRole.DATA_OWNER, set()):
        reason = RevokeReason.OWNER
    else:
        raise ServiceError("Only the person, the data owner or an admin can remove this access.")
    after = mark_for_revoke(grant, reason, user, note)
    if after is None:
        raise ServiceError("This access is already being removed.")
    return after


@_service
def tasks_for_user(user: CurrentUser) -> list[dict]:
    """Pending approval tasks this user may decide (incl. as delegate)."""
    tasks = fetch_all("SELECT * FROM v_task WHERE task_status = 'PENDING' ORDER BY due_at")
    if not tasks:
        return []
    delegations = active_delegations()
    prior = _prior_approvers([t["request_id"] for t in tasks])
    groups: dict[str, dict] = {}
    out = []
    for t in tasks:
        group = groups.setdefault(t["group_code"], get_group(t["group_code"]))
        ctx = approval_context(t, group, delegations, prior.get(t["request_id"], set()))
        ok, _, on_behalf = workflow.can_decide(user.email, user.app_roles, t["approver_role"], ctx)
        if ok:
            out.append({**t, "on_behalf_of": on_behalf})
    return out


# ---------------------------------------------------------------------------
# Recertification decisions
# ---------------------------------------------------------------------------
@_service
def recert_decide(user: CurrentUser, item_id: str, keep: bool, comment: str | None = None) -> None:
    comment = (comment or "").strip()
    if not keep and len(comment) < 5:
        raise ServiceError("Say why the access should be removed.")
    item = _must(fetch_one("SELECT * FROM recert_item WHERE item_id = :id", {"id": item_id}), "Review item")
    if item["decision"] != "PENDING":
        raise ServiceError("This item has already been reviewed.")
    delegates = active_delegations().get(item["reviewer_email"], set())
    if user.email != item["reviewer_email"] and user.email not in delegates:
        raise ServiceError("You are not the reviewer (or their delegate) for this item.")
    grant = _grant(item["grant_id"])
    if user.email == grant["user_email"]:
        raise ServiceError("You cannot review your own access.")
    decision = "KEEP" if keep else "REVOKE"
    n = db.execute("UPDATE recert_item SET decision = :d, decided_by = :by, decided_at = current_timestamp(), "
                   "decision_comment = :c WHERE item_id = :id AND decision = 'PENDING'",
                   {"d": decision, "by": user.email, "c": comment or None, "id": item_id})
    if n != 1:
        raise ServiceError("This item has already been reviewed.")
    audit.record(user, "RECERT_DECIDED", "RECERT_ITEM", item_id, before=item,
                 after={**item, "decision": decision, "decided_by": user.email, "decision_comment": comment or None},
                 correlation_id=grant["request_id"])
    if grant["status"] == GrantStatus.ACTIVE.value:
        if keep:
            db.execute("UPDATE access_grant SET last_recertified_at = current_timestamp(), "
                       "updated_at = current_timestamp() WHERE grant_id = :id AND status = 'ACTIVE'",
                       {"id": grant["grant_id"]})
        else:
            mark_for_revoke(grant, RevokeReason.RECERT_REVOKE, user, comment)


# ---------------------------------------------------------------------------
# Manual fulfilment (Catalog admin app). An admin changes the group by hand, then records it here.
# ---------------------------------------------------------------------------
@_service
def mark_granted(user: CurrentUser, grant_id: str, note: str | None = None) -> dict:
    """The admin has added the person to the group. Access ends on the date approved; emergency access
    gets its full hours from now."""
    _require(user, AppRole.ADMIN)
    grant = _must(fetch_one("SELECT * FROM v_grant WHERE grant_id = :id", {"id": grant_id}), "Grant")
    if grant["status"] != GrantStatus.PENDING_PROVISION.value:
        raise ServiceError("This access is not waiting to be granted.")
    if grant["user_email"] == user.email:
        raise ServiceError("You cannot record access given to yourself; ask another admin.")
    if get_group(grant["group_code"])["status"] != "ACTIVE":
        raise ServiceError(f"{grant['group_name']} is being retired; this access has been withdrawn.")
    now = db.now()
    if grant["is_break_glass"]:
        valid_from, valid_to = now, now + (grant["valid_to"] - grant["valid_from"])
    else:
        valid_from, valid_to = grant["valid_from"], grant["valid_to"]
        if valid_to <= now:
            withdraw_pending_grant(grant, RevokeReason.EXPIRED, user, "End date passed before the member was added")
            raise ServiceError(f"This access was approved until {valid_to:%d-%m-%Y}, which has passed, so it has "
                               "been closed. Do not add the person; they can raise a new request.")
    n = db.execute(
        "UPDATE access_grant SET status = :new, valid_from = :from, valid_to = :to, provisioned_at = :now, "
        "provisioned_by = :by, fulfilment_note = :note, updated_at = current_timestamp() "
        "WHERE grant_id = :id AND status = 'PENDING_PROVISION'",
        {"new": workflow.next_grant_status(grant["status"], "granted"), "now": now, "from": valid_from, "to": valid_to,
         "by": user.email, "note": (note or "").strip() or None, "id": grant_id})
    if n != 1:
        raise ServiceError(STALE)
    req = fetch_one("SELECT status FROM access_request WHERE request_id = :id", {"id": grant["request_id"]})
    if req["status"] == RequestStatus.APPROVED.value:
        set_request_status(grant["request_id"], req["status"],
                           workflow.next_request_status(req["status"], "granted", "admin"), fulfilled_at=now)
    after = fetch_one("SELECT * FROM v_grant WHERE grant_id = :id", {"id": grant_id})
    audit.record(user, "GRANT_FULFILLED", "ACCESS_GRANT", grant_id, before=grant, after=after,
                 reason=(note or "").strip() or "Member added by hand", correlation_id=grant["request_ref"])
    notify.enqueue([grant["user_email"]], f"Access granted: {grant['group_name']}",
                   f"You now have access to {access_label(grant)} through group {grant['group_name']}, "
                   f"until {valid_to:%d %b %Y %H:%M} UTC.", "ACCESS_GRANTED", request_id=grant["request_id"])
    return after


@_service
def mark_removed(user: CurrentUser, grant_id: str, note: str | None = None) -> dict:
    """The admin has removed the person from the group (access being revoked, or past its end date)."""
    _require(user, AppRole.ADMIN)
    grant = _must(fetch_one("SELECT * FROM v_grant WHERE grant_id = :id", {"id": grant_id}), "Grant")
    now = db.now()
    note = (note or "").strip() or None
    if grant["status"] == GrantStatus.ACTIVE.value and grant["valid_to"] <= now:
        # Ended but not yet picked up by the daily job: close it as expired in one step
        n = db.execute(
            "UPDATE access_grant SET status = 'EXPIRED', revoke_reason = 'EXPIRED', revoke_requested_at = valid_to, "
            "revoked_by = :by, revoked_at = :now, removed_by = :by, removal_note = :note, "
            "updated_at = current_timestamp() WHERE grant_id = :id AND status = 'ACTIVE' AND valid_to <= :now",
            {"by": user.email, "now": now, "note": note, "id": grant_id})
    elif grant["status"] == GrantStatus.PENDING_REVOKE.value:
        n = db.execute(
            "UPDATE access_grant SET status = :new, revoked_at = :now, removed_by = :by, removal_note = :note, "
            "updated_at = current_timestamp() WHERE grant_id = :id AND status = 'PENDING_REVOKE'",
            {"new": workflow.next_grant_status(grant["status"], "removed", grant["revoke_reason"]), "now": now,
             "by": user.email, "note": note, "id": grant_id})
    else:
        raise ServiceError("This access is not waiting to be removed.")
    if n != 1:
        raise ServiceError(STALE)
    after = fetch_one("SELECT * FROM v_grant WHERE grant_id = :id", {"id": grant_id})
    audit.record(user, "GRANT_REMOVED", "ACCESS_GRANT", grant_id, before=grant, after=after,
                 reason=note or "Member removed by hand", correlation_id=grant["request_ref"])
    notify.enqueue([grant["user_email"]], f"Access removed: {grant['group_name']}",
                   f"Your access to {access_label(grant)} (group {grant['group_name']}) has been removed.",
                   "ACCESS_REMOVED", request_id=grant["request_id"])
    retire_if_empty(user, grant["group_code"])
    return after


def retire_if_empty(actor: CurrentUser | str, group_code: str) -> bool:
    """RETIRING -> RETIRED once nobody is (or is about to be) in the group."""
    busy = fetch_one("SELECT count(*) AS n FROM access_grant WHERE group_code = :g "
                     "AND status IN ('PENDING_PROVISION', 'ACTIVE', 'PENDING_REVOKE')", {"g": group_code})
    if busy["n"] or db.execute("UPDATE access_group SET status = 'RETIRED', updated_at = current_timestamp() "
                               "WHERE group_code = :g AND status = 'RETIRING'", {"g": group_code}) != 1:
        return False
    audit.record(actor, "GROUP_RETIRED", "ACCESS_GROUP", group_code)
    return True


# ---------------------------------------------------------------------------
# Catalog administration (Catalog admin app). Identifiers are generated here.
# ---------------------------------------------------------------------------
_UC_NAME_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,254}$")


def databricks_workspace_id(host_url: str) -> int | None:
    """Azure workspace URLs carry the id: https://adb-<id>.<n>.azuredatabricks.net"""
    match = re.match(r"^https://adb-(\d+)\.", host_url or "")
    return int(match.group(1)) if match else None


def _workspace_values(name: str, workspace_type: str, host_url: str, environment: str) -> dict:
    name = (name or "").strip()
    host = (host_url or "").strip().rstrip("/").lower()
    if not name:
        raise ServiceError("Give the workspace a name.")
    if workspace_type not in WORKSPACE_TYPES:
        raise ServiceError(f"Workspace type must be one of {', '.join(WORKSPACE_TYPES)}.")
    if not re.match(r"^https://[a-z0-9.-]+$", host):
        raise ServiceError("The workspace URL must look like https://adb-1234567890123456.7.azuredatabricks.net")
    if environment not in ENVIRONMENTS:
        raise ServiceError("Environment must be dev, tst or prd.")
    return {"name": name, "type": workspace_type, "host": host, "env": environment,
            "wsid": databricks_workspace_id(host)}


@_service
def register_workspace(user: CurrentUser, *, name: str, workspace_type: str, host_url: str,
                       environment: str) -> dict:
    _require(user, AppRole.ADMIN)
    v = _workspace_values(name, workspace_type, host_url, environment)
    n = db.execute(
        f"""
        INSERT INTO workspace (workspace_code, workspace_seq, name, workspace_type, host_url, databricks_workspace_id,
                               environment, is_active, created_by, created_at, updated_at)
        SELECT {_next_code('WS', 4)}, s.seq, :name, :type, :host, CAST(:wsid AS BIGINT), :env, true, :by,
               current_timestamp(), current_timestamp()
        FROM (SELECT coalesce(max(workspace_seq), 0) + 1 AS seq FROM workspace) s
        WHERE NOT EXISTS (SELECT 1 FROM workspace WHERE lower(name) = lower(:name) OR host_url = :host)
        """, {**v, "by": user.email})
    if n != 1:
        raise ServiceError("A workspace with this name or URL is already registered.")
    row = fetch_one("SELECT * FROM workspace WHERE host_url = :host", v)
    audit.record(user, "WORKSPACE_REGISTERED", "WORKSPACE", row["workspace_code"], after=row)
    return row


@_service
def update_workspace(user: CurrentUser, workspace_code: str, *, name: str, workspace_type: str, host_url: str,
                     environment: str, is_active: bool) -> dict:
    _require(user, AppRole.ADMIN)
    before = _must(fetch_one("SELECT * FROM workspace WHERE workspace_code = :c", {"c": workspace_code}), "Workspace")
    v = _workspace_values(name, workspace_type, host_url, environment)
    if fetch_one("SELECT 1 AS x FROM workspace WHERE workspace_code <> :c AND (lower(name) = lower(:name) "
                 "OR host_url = :host)", {"c": workspace_code, **v}):
        raise ServiceError("Another workspace already has this name or URL.")
    db.execute("UPDATE workspace SET name = :name, workspace_type = :type, host_url = :host, "
               "databricks_workspace_id = CAST(:wsid AS BIGINT), environment = :env, is_active = :active, "
               "updated_by = :by, updated_at = current_timestamp() WHERE workspace_code = :c",
               {**v, "active": bool(is_active), "by": user.email, "c": workspace_code})
    after = fetch_one("SELECT * FROM workspace WHERE workspace_code = :c", {"c": workspace_code})
    audit.record(user, "WORKSPACE_UPDATED", "WORKSPACE", workspace_code, before=before, after=after)
    return after


def _data_product_values(name: str, description: str | None, data_product_type: str, project: str | None,
                         catalog: str | None, schema: str | None, classification_code: str, owner_email: str,
                         steward_email: str, home_workspace_code: str | None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ServiceError("Give the data product a name.")
    if data_product_type not in DATA_PRODUCT_TYPES:
        raise ServiceError(f"Type must be one of {', '.join(DATA_PRODUCT_TYPES.values())}.")
    catalog = (catalog or "").strip().lower() or None
    schema = (schema or "").strip().lower() or None
    if data_product_type == "SCHEMA" and not (catalog and schema):
        raise ServiceError("A schema data product needs its catalog and schema.")
    if schema and not catalog:
        raise ServiceError("Give the catalog the schema is in.")
    for what, value in (("Catalog", catalog), ("Schema", schema)):
        if value and not _UC_NAME_RE.match(value):
            raise ServiceError(f"{what} name {value!r} may use only letters, digits, _ and -.")
    if classification_code not in CLASSIFICATIONS:
        raise ServiceError(f"Classification must be one of {', '.join(CLASSIFICATIONS)}.")
    owner, steward = _email(owner_email, "Data owner email"), _email(steward_email, "Steward email")
    if classification_code != "GENERAL" and owner == steward:
        raise ServiceError("Data owner and steward must be different people for confidential or restricted data.")
    if home_workspace_code and not fetch_one("SELECT 1 AS x FROM workspace WHERE workspace_code = :c",
                                             {"c": home_workspace_code}):
        raise ServiceError(f"Workspace {home_workspace_code} is not registered.")
    return {"name": name, "description": (description or "").strip() or None, "type": data_product_type,
            "project": (project or "").strip() or None, "catalog": catalog, "schema": schema,
            "cls": classification_code, "owner": owner, "steward": steward, "home": home_workspace_code or None}


# Another data product with the same name, or (schema type) the same schema
_DP_CLASH = ("data_product_code <> :c AND (lower(name) = lower(:name) OR (:type = 'SCHEMA' "
             "AND data_product_type = 'SCHEMA' AND catalog_name = :catalog AND schema_name = :schema))")


def _check_data_product_clash(v: dict, code: str) -> None:
    clash = fetch_one(f"SELECT name, data_product_code FROM data_product WHERE {_DP_CLASH} LIMIT 1", {**v, "c": code})
    if clash and clash["name"].lower() == v["name"].lower():
        raise ServiceError(f"A data product named {clash['name']} is already registered ({clash['data_product_code']}).")
    if clash:
        raise ServiceError(f"{v['catalog']}.{v['schema']} is already registered as data product "
                           f"{clash['data_product_code']} {clash['name']}.")


@_service
def register_data_product(user: CurrentUser, *, name: str, description: str | None, data_product_type: str,
                          project: str | None = None, catalog: str | None = None, schema: str | None = None,
                          classification_code: str, owner_email: str, steward_email: str,
                          home_workspace_code: str | None = None) -> dict:
    """Registers the data product only; groups are registered separately (FR-3, FR-9)."""
    _require(user, AppRole.ADMIN)
    v = _data_product_values(name, description, data_product_type, project, catalog, schema, classification_code,
                             owner_email, steward_email, home_workspace_code)
    _check_data_product_clash(v, "")
    ensure_user(v["owner"])
    ensure_user(v["steward"])
    n = db.execute(
        f"""
        INSERT INTO data_product (data_product_code, data_product_seq, name, description, data_product_type,
                                  project_name, catalog_name, schema_name, classification_code, home_workspace_code,
                                  owner_email, steward_email, lifecycle_status, created_by, created_at, updated_at)
        SELECT {_next_code('DP', 4)}, s.seq, :name, CAST(:description AS STRING), :type, CAST(:project AS STRING),
               CAST(:catalog AS STRING), CAST(:schema AS STRING), :cls, CAST(:home AS STRING), :owner, :steward,
               'ACTIVE', :by, current_timestamp(), current_timestamp()
        FROM (SELECT coalesce(max(data_product_seq), 0) + 1 AS seq FROM data_product) s
        WHERE NOT EXISTS (SELECT 1 FROM data_product WHERE {_DP_CLASH})
        """, {**v, "c": "", "by": user.email})
    if n != 1:
        raise ServiceError(STALE)
    row = fetch_one("SELECT * FROM data_product WHERE lower(name) = lower(:name)", v)
    audit.record(user, "DATA_PRODUCT_REGISTERED", "DATA_PRODUCT", row["data_product_code"], after=row)
    return row


@_service
def update_data_product(user: CurrentUser, data_product_code: str, *, name: str, description: str | None,
                        data_product_type: str, project: str | None = None, catalog: str | None = None,
                        schema: str | None = None, classification_code: str, owner_email: str, steward_email: str,
                        home_workspace_code: str | None, lifecycle_status: str = "ACTIVE") -> dict:
    """Edit any field. A new owner / steward is audited with old and new values (FR-12)."""
    _require(user, AppRole.ADMIN)
    before = _must(fetch_one("SELECT * FROM data_product WHERE data_product_code = :c", {"c": data_product_code}),
                   "Data product")
    if lifecycle_status not in ("ACTIVE", "RETIRED"):
        raise ServiceError("Status must be ACTIVE or RETIRED.")
    v = _data_product_values(name, description, data_product_type, project, catalog, schema, classification_code,
                             owner_email, steward_email, home_workspace_code)
    _check_data_product_clash(v, data_product_code)
    ensure_user(v["owner"])
    ensure_user(v["steward"])
    db.execute(
        "UPDATE data_product SET name = :name, description = :description, data_product_type = :type, "
        "project_name = :project, catalog_name = :catalog, schema_name = :schema, classification_code = :cls, "
        "home_workspace_code = :home, owner_email = :owner, steward_email = :steward, lifecycle_status = :status, "
        "updated_by = :by, updated_at = current_timestamp() WHERE data_product_code = :c",
        {**v, "status": lifecycle_status, "by": user.email, "c": data_product_code})
    after = fetch_one("SELECT * FROM data_product WHERE data_product_code = :c", {"c": data_product_code})
    audit.record(user, "DATA_PRODUCT_UPDATED", "DATA_PRODUCT", data_product_code, before=before, after=after)
    return after


def check_group(*, group_name: str, data_product_code: str | None, group_code: str | None = None) -> list[dict]:
    """The checks a group must pass before it is saved: [{check, ok, detail}].
    Shown to the admin by "Validate"; saving runs them again. Whether the group really exists
    in Entra ID / Databricks is checked by the admin, not the app."""
    out = []
    try:
        name = naming.validate_group_name(group_name)
        out.append({"check": "Name format", "ok": True, "detail": name})
    except naming.NamingError as exc:
        name = None
        out.append({"check": "Name format", "ok": False, "detail": str(exc)})
    if name:
        clash = fetch_one("SELECT group_code, group_name FROM access_group WHERE lower(group_name) = lower(:n) "
                          "AND group_code <> :g", {"n": name, "g": group_code or ""})
        out.append({"check": "Not already registered", "ok": clash is None,
                    "detail": f"{clash['group_code']} is already {clash['group_name']}" if clash else "No clash"})
    dp = fetch_one("SELECT * FROM data_product WHERE data_product_code = :c", {"c": data_product_code or ""})
    if dp is None:
        out.append({"check": "Data product", "ok": False, "detail": "Choose a registered data product."})
    else:
        out.append({"check": "Data product", "ok": dp["lifecycle_status"] == "ACTIVE",
                    "detail": f"{dp['data_product_code']} {dp['name']}"
                              + ("" if dp["lifecycle_status"] == "ACTIVE" else " is retired")})
    return out


def _require_checks(checks: list[dict]) -> None:
    failed = [c for c in checks if not c["ok"]]
    if failed:
        raise ServiceError("; ".join(f"{c['check']}: {c['detail']}" for c in failed))


@_service
def register_group(user: CurrentUser, *, group_name: str, data_product_code: str, description: str | None = None,
                   requestable: bool = True) -> dict:
    """Record an existing group and link it to one data product (FR-9, FR-10). It is active at once."""
    _require(user, AppRole.ADMIN)
    _require_checks(check_group(group_name=group_name, data_product_code=data_product_code))
    group_name = naming.validate_group_name(group_name)
    dp = fetch_one("SELECT name FROM data_product WHERE data_product_code = :c", {"c": data_product_code})
    n = db.execute(
        f"""
        INSERT INTO access_group (group_code, group_seq, group_name, data_product_code, status, requestable,
                                  description, created_by, created_at, updated_at)
        SELECT {_next_code('GRP', 4)}, s.seq, :name, :dp, 'ACTIVE', :requestable, CAST(:description AS STRING),
               :by, current_timestamp(), current_timestamp()
        FROM (SELECT coalesce(max(group_seq), 0) + 1 AS seq FROM access_group) s
        WHERE NOT EXISTS (SELECT 1 FROM access_group WHERE lower(group_name) = lower(:name))
        """, {"name": group_name, "dp": data_product_code, "requestable": bool(requestable),
              "description": (description or "").strip() or f"Access to data product {dp['name']}",
              "by": user.email})
    if n != 1:
        raise ServiceError(f"A group named {group_name} is already registered.")
    row = fetch_one("SELECT * FROM access_group WHERE lower(group_name) = lower(:name)", {"name": group_name})
    audit.record(user, "GROUP_REGISTERED", "ACCESS_GROUP", row["group_code"], after=row)
    return row


@_service
def update_group(user: CurrentUser, group_code: str, *, group_name: str, data_product_code: str,
                 description: str | None, requestable: bool) -> dict:
    """Edit an active group. It can move to another data product only while nobody has or is asking for it."""
    _require(user, AppRole.ADMIN)
    before = _must(fetch_one("SELECT * FROM access_group WHERE group_code = :g", {"g": group_code}), "Access group")
    if before["status"] != "ACTIVE":
        raise ServiceError(f"{before['group_name']} is {before['status'].lower()} and can no longer be edited.")
    _require_checks(check_group(group_name=group_name, data_product_code=data_product_code, group_code=group_code))
    if data_product_code != before["data_product_code"] and fetch_one(
            "SELECT 1 AS x FROM access_grant WHERE group_code = :g AND status IN (:live) UNION ALL "
            "SELECT 1 AS x FROM access_request WHERE group_code = :g AND status IN (:open)",
            {"g": group_code, "live": list(LIVE_GRANT_STATES), "open": list(OPEN_GRANT_REQUEST_STATES)}):
        raise ServiceError("This group has members or open requests, so it stays linked to its data product. "
                           "Retire it and register a new group instead.")
    n = db.execute("UPDATE access_group SET group_name = :n, data_product_code = :dp, description = :d, "
                   "requestable = :r, updated_by = :by, updated_at = current_timestamp() "
                   "WHERE group_code = :g AND status = 'ACTIVE'",
                   {"n": naming.validate_group_name(group_name), "dp": data_product_code,
                    "d": (description or "").strip() or None, "r": bool(requestable), "by": user.email,
                    "g": group_code})
    if n != 1:
        raise ServiceError(STALE)
    after = fetch_one("SELECT * FROM access_group WHERE group_code = :g", {"g": group_code})
    audit.record(user, "GROUP_UPDATED", "ACCESS_GROUP", group_code, before=before, after=after)
    return after


@_service
def retire_group(user: CurrentUser, group_code: str, reason: str) -> None:
    """Stop new requests; current members are listed for removal by hand; RETIRED once the last one is removed."""
    _require(user, AppRole.ADMIN)
    if len((reason or "").strip()) < 10:
        raise ServiceError("Give a reason for retiring the group (at least 10 characters).")
    before = _must(fetch_one("SELECT * FROM access_group WHERE group_code = :g", {"g": group_code}), "Access group")
    if before["status"] != "ACTIVE":
        raise ServiceError(f"{before['group_name']} is already {before['status'].lower()}.")
    n = db.execute("UPDATE access_group SET status = 'RETIRING', requestable = false, updated_by = :by, "
                   "updated_at = current_timestamp() WHERE group_code = :g AND status = 'ACTIVE'",
                   {"by": user.email, "g": group_code})
    if n != 1:
        raise ServiceError(STALE)
    audit.record(user, "GROUP_RETIRING", "ACCESS_GROUP", group_code, before=before, reason=reason)
    for grant in fetch_all("SELECT * FROM access_grant WHERE group_code = :g AND status = 'ACTIVE'", {"g": group_code}):
        mark_for_revoke(grant, RevokeReason.GROUP_RETIRED, user, reason)
    for grant in fetch_all("SELECT * FROM access_grant WHERE group_code = :g AND status = 'PENDING_PROVISION'",
                           {"g": group_code}):
        withdraw_pending_grant(grant, RevokeReason.GROUP_RETIRED, user, reason)
    open_ids = [r["request_id"] for r in fetch_all(
        "SELECT request_id FROM access_request WHERE group_code = :g AND status IN ('DRAFT', 'PENDING_APPROVAL')",
        {"g": group_code})]
    if open_ids:
        db.execute("UPDATE approval_task SET status = 'CANCELLED' WHERE status = 'PENDING' AND request_id IN (:ids)",
                   {"ids": open_ids})
        db.execute("UPDATE access_request SET status = 'CANCELLED', decided_at = current_timestamp(), "
                   "version = version + 1, updated_at = current_timestamp() WHERE request_id IN (:ids) "
                   "AND status IN ('DRAFT', 'PENDING_APPROVAL')", {"ids": open_ids})
    retire_if_empty(user, group_code)


@_service
def add_delegation(user: CurrentUser, delegator_email: str, delegate_email: str, valid_to: datetime,
                   reason: str) -> None:
    delegator, delegate = _email(delegator_email), _email(delegate_email, "Delegate email")
    if delegator != user.email:
        _require(user, AppRole.ADMIN)
    if delegator == delegate:
        raise ServiceError("Choose someone other than yourself.")
    if len((reason or "").strip()) < 5:
        raise ServiceError("Give a reason for the delegation (for example annual leave).")
    now = db.now()
    if not now < valid_to <= now + timedelta(days=60):
        raise ServiceError("A delegation must end in the future and within 60 days.")
    ensure_user(delegator)
    ensure_user(delegate)
    row = {"delegation_id": str(uuid.uuid4()), "delegator_email": delegator, "delegate_email": delegate,
           "valid_from": now, "valid_to": valid_to, "reason": reason.strip(), "created_by": user.email}
    db.execute("INSERT INTO delegation (delegation_id, delegator_email, delegate_email, valid_from, valid_to, reason, "
               "created_by, created_at) VALUES (:delegation_id, :delegator_email, :delegate_email, :valid_from, "
               ":valid_to, :reason, :created_by, current_timestamp())", row)
    audit.record(user, "DELEGATION_CREATED", "DELEGATION", row["delegation_id"], after=row)


def verify_audit_chain() -> list[dict]:
    return audit.verify_chain()
