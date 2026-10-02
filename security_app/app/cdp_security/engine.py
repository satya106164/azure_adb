"""Provisioning engine - turns desired state in Lakebase into real changes.

    plan()     derive provisioning_action rows from groups, grants and entitlements
    claim()    take due actions with FOR UPDATE SKIP LOCKED (safe if two runs overlap)
    execute    call the provider OUTSIDE any DB transaction, then record the outcome

External calls are idempotent, so an action that is retried after a crash
between "call succeeded" and "outcome recorded" is harmless.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from . import audit, notify, workflow
from .config import get_settings
from .db import transaction
from .models import CurrentUser, GrantStatus, RequestStatus
from .providers.base import DeferError, PermanentError

log = logging.getLogger(__name__)
ACTOR = CurrentUser.system("provisioning")
MAX_ATTEMPTS = 6
DEFER_MINUTES = 10


@dataclass
class Providers:
    graph: Any = None       # EntraGraphProvider
    uc: Any = None          # UCGrantProvider
    ws_acl: Any = None      # WorkspaceAclProvider
    pbi: Any = None         # PowerBIProvider
    dry_run: bool = True


def backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(5 * 2 ** max(attempts - 1, 0), 360))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def reap_stale(cur) -> int:
    cur.execute("UPDATE provisioning_action SET status = 'FAILED', last_error = 'stale: run died mid-action', "
                "next_attempt_at = now() WHERE status = 'IN_PROGRESS' AND started_at < now() - interval '30 minutes'")
    return cur.rowcount


def plan(cur) -> dict[str, int]:
    counts: dict[str, int] = {}
    if get_settings().graph_create_groups:
        cur.execute("""
            INSERT INTO provisioning_action (action_type, access_group_id, idempotency_key, created_by)
            SELECT 'CREATE_GROUP', g.access_group_id, 'CREATE_GROUP:' || g.access_group_id, 'system:planner'
            FROM access_group g WHERE g.status = 'PLANNED' AND g.entra_object_id IS NULL
            ON CONFLICT (idempotency_key) DO NOTHING""")
        counts["CREATE_GROUP"] = cur.rowcount
    cur.execute("""
        INSERT INTO provisioning_action (action_type, grant_id, access_group_id, idempotency_key, created_by)
        SELECT 'ADD_MEMBER', g.grant_id, g.access_group_id, 'ADD_MEMBER:' || g.grant_id, 'system:planner'
        FROM access_grant g WHERE g.status = 'PENDING_PROVISION'
        ON CONFLICT (idempotency_key) DO NOTHING""")
    counts["ADD_MEMBER"] = cur.rowcount
    cur.execute("""
        INSERT INTO provisioning_action (action_type, grant_id, access_group_id, idempotency_key, created_by)
        SELECT 'REMOVE_MEMBER', g.grant_id, g.access_group_id, 'REMOVE_MEMBER:' || g.grant_id, 'system:planner'
        FROM access_grant g WHERE g.status = 'PENDING_REVOKE'
        ON CONFLICT (idempotency_key) DO NOTHING""")
    counts["REMOVE_MEMBER"] = cur.rowcount
    cur.execute("""
        INSERT INTO provisioning_action (action_type, entitlement_id, access_group_id, idempotency_key, created_by)
        SELECT CASE WHEN e.desired_state = 'PRESENT' THEN 'APPLY_ENTITLEMENT' ELSE 'REVOKE_ENTITLEMENT' END,
               e.entitlement_id, e.access_group_id,
               (CASE WHEN e.desired_state = 'PRESENT' THEN 'APPLY:' ELSE 'REVOKE:' END)
                 || e.entitlement_id || ':' || to_char(e.updated_at, 'YYYYMMDDHH24MISSUS'),
               'system:planner'
        FROM group_entitlement e
        WHERE e.apply_status = 'PENDING'
          AND NOT EXISTS (SELECT 1 FROM provisioning_action a WHERE a.entitlement_id = e.entitlement_id
                          AND a.status IN ('QUEUED','IN_PROGRESS','FAILED'))
        ON CONFLICT (idempotency_key) DO NOTHING""")
    counts["ENTITLEMENT"] = cur.rowcount
    # Requests whose grant is now queued move to PROVISIONING
    cur.execute("""
        UPDATE access_request r SET status = 'PROVISIONING', version = version + 1
        WHERE r.status = 'APPROVED' AND r.request_type IN ('GRANT','BREAK_GLASS')
          AND EXISTS (SELECT 1 FROM access_grant g WHERE g.request_id = r.request_id
                      AND g.status = 'PENDING_PROVISION')""")
    return counts


def claim(limit: int) -> list[dict]:
    with transaction() as cur:
        cur.execute("""
            UPDATE provisioning_action SET status = 'IN_PROGRESS', attempts = attempts + 1, started_at = now()
            WHERE action_id IN (
                SELECT action_id FROM provisioning_action
                WHERE status IN ('QUEUED','FAILED') AND next_attempt_at <= now()
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s)
            RETURNING *""", (limit,))
        return cur.fetchall()


def _complete(cur, action: dict, note: str | None = None) -> None:
    cur.execute("UPDATE provisioning_action SET status = 'SUCCEEDED', completed_at = now(), last_error = %s "
                "WHERE action_id = %s", (note, action["action_id"]))


# ---------------------------------------------------------------------------
# Handlers - each: read (short txn) -> external call -> record (short txn)
# ---------------------------------------------------------------------------
def _create_group(action: dict, p: Providers) -> None:
    with transaction() as cur:
        cur.execute("SELECT g.*, u.email AS owner_email FROM access_group g "
                    "LEFT JOIN app_user u ON u.user_id = g.owner_user_id WHERE g.access_group_id = %s",
                    (action["access_group_id"],))
        g = cur.fetchone()
    if g["status"] != "PLANNED" or g["entra_object_id"]:
        with transaction() as cur:
            _complete(cur, action, "no-op: group already active")
        return
    if p.dry_run:
        oid = f"dryrun-{uuid.uuid4()}"
    else:
        owner_ids = []
        if g["owner_email"]:
            owner = p.graph.resolve_user(g["owner_email"])
            owner_ids = [owner["id"]] if owner else []
        oid = p.graph.create_security_group(g["group_name"], g["description"] or g["group_name"], owner_ids)
    with transaction() as cur:
        cur.execute("UPDATE access_group SET entra_object_id = %s, status = 'ACTIVE' WHERE access_group_id = %s "
                    "RETURNING *", (oid, g["access_group_id"]))
        audit.record(cur, ACTOR, "GROUP_CREATED", "ACCESS_GROUP", g["access_group_id"], before=g,
                     after=cur.fetchone(), reason="dry run" if p.dry_run else None)
        _complete(cur, action)


def _grant_row(action: dict) -> dict:
    with transaction() as cur:
        cur.execute("""
            SELECT gr.*, u.email, u.entra_object_id AS user_object_id, ag.group_name,
                   ag.entra_object_id AS group_object_id, ag.status AS group_status, r.request_ref,
                   r.status AS request_status, rq.email AS requester_email
            FROM access_grant gr
            JOIN app_user u ON u.user_id = gr.user_id
            JOIN access_group ag ON ag.access_group_id = gr.access_group_id
            JOIN access_request r ON r.request_id = gr.request_id
            JOIN app_user rq ON rq.user_id = r.requester_user_id
            WHERE gr.grant_id = %s""", (action["grant_id"],))
        return cur.fetchone()


def _resolve_user_id(p: Providers, row: dict) -> str | None:
    user = p.graph.resolve_user(row["email"])
    if not user:
        return None
    if row["user_object_id"] != user["id"]:
        with transaction() as cur:
            cur.execute("UPDATE app_user SET entra_object_id = %s WHERE email = %s", (user["id"], row["email"]))
    return user["id"] if user.get("accountEnabled", True) else None


def _add_member(action: dict, p: Providers) -> None:
    row = _grant_row(action)
    if row["status"] != GrantStatus.PENDING_PROVISION.value:
        with transaction() as cur:
            _complete(cur, action, f"no-op: grant is {row['status']}")
        return
    if row["group_status"] != "ACTIVE" or not row["group_object_id"]:
        raise DeferError("Group not active in Entra yet")
    if not p.dry_run:
        user_id = _resolve_user_id(p, row)
        if not user_id:
            raise PermanentError(f"{row['email']} not found or disabled in Entra ID")
        p.graph.add_member(row["group_object_id"], user_id)
    with transaction() as cur:
        new = workflow.next_grant_status(row["status"], "provisioned")
        cur.execute("UPDATE access_grant SET status = %s, provisioned_at = now() WHERE grant_id = %s RETURNING *",
                    (new.value, row["grant_id"]))
        after = cur.fetchone()
        if row["request_status"] in (RequestStatus.PROVISIONING.value, RequestStatus.APPROVED.value):
            status = RequestStatus.PROVISIONING if row["request_status"] == "APPROVED" else RequestStatus(row["request_status"])
            final = workflow.next_request_status(status, "provisioned", "system")
            cur.execute("UPDATE access_request SET status = %s, fulfilled_at = now(), version = version + 1 "
                        "WHERE request_id = %s", (final.value, row["request_id"]))
        audit.record(cur, ACTOR, "MEMBER_ADDED", "ACCESS_GRANT", row["grant_id"], after=after,
                     reason="dry run" if p.dry_run else None, correlation_id=row["request_ref"])
        notify.enqueue(cur, [row["email"], row["requester_email"]], f"Access ready: {row['group_name']}",
                       f"{row['email']} is now a member of {row['group_name']} until {row['valid_to']:%d %b %Y}.\n"
                       "It can take up to an hour for Databricks and Power BI to pick up the change.",
                       "ACCESS_READY", request_id=row["request_id"], dedupe_key=f"ready:{row['grant_id']}")
        _complete(cur, action)


def _remove_member(action: dict, p: Providers) -> None:
    row = _grant_row(action)
    if row["status"] != GrantStatus.PENDING_REVOKE.value:
        with transaction() as cur:
            _complete(cur, action, f"no-op: grant is {row['status']}")
        return
    note = None
    if not p.dry_run and row["group_object_id"] and not row["group_object_id"].startswith("dryrun-"):
        user = p.graph.resolve_user(row["email"])
        if user:
            p.graph.remove_member(row["group_object_id"], user["id"])
        else:
            note = "user no longer exists in Entra ID; treated as removed"
    with transaction() as cur:
        new = workflow.next_grant_status(row["status"], "removed", row["revoke_reason"])
        cur.execute("UPDATE access_grant SET status = %s, revoked_at = now() WHERE grant_id = %s RETURNING *",
                    (new.value, row["grant_id"]))
        audit.record(cur, ACTOR, "MEMBER_REMOVED", "ACCESS_GRANT", row["grant_id"], after=cur.fetchone(),
                     reason=note or ("dry run" if p.dry_run else row["revoke_reason"]),
                     correlation_id=row["request_ref"])
        _complete(cur, action, note)


def _entitlement_row(action: dict) -> dict:
    with transaction() as cur:
        cur.execute("""
            SELECT e.*, ag.group_name, ag.status AS group_status, ag.entra_object_id,
                   s.securable_type, s.full_name, a.asset_type, a.external_id, w.host_url
            FROM group_entitlement e
            JOIN access_group ag ON ag.access_group_id = e.access_group_id
            LEFT JOIN uc_securable s ON s.securable_id = e.securable_id
            LEFT JOIN asset a ON a.asset_id = e.asset_id
            LEFT JOIN workspace w ON w.workspace_id = a.workspace_id
            WHERE e.entitlement_id = %s""", (action["entitlement_id"],))
        return cur.fetchone()


def _entitlement(action: dict, p: Providers, apply: bool) -> None:
    e = _entitlement_row(action)
    wanted = "PRESENT" if apply else "ABSENT"
    if e["desired_state"] != wanted or e["apply_status"] != "PENDING":
        with transaction() as cur:
            _complete(cur, action, "no-op: desired state changed; planner will re-queue")
        return
    if apply and e["group_status"] != "ACTIVE":
        raise DeferError("Group not active yet")
    if not p.dry_run:
        verb = "grant" if apply else "revoke"
        if e["securable_type"]:
            getattr(p.uc, verb)(e["securable_type"], e["full_name"], e["privilege"], e["group_name"])
        elif e["asset_type"].startswith("PBI_"):
            if not p.pbi:
                raise PermanentError("Power BI provider is not configured")
            getattr(p.pbi, verb)(e["asset_type"], e["external_id"], e["entra_object_id"], e["privilege"])
        else:
            getattr(p.ws_acl, verb)(e["host_url"], e["asset_type"], e["external_id"], e["group_name"], e["privilege"])
    with transaction() as cur:
        cur.execute("UPDATE group_entitlement SET apply_status = %s, last_applied_at = now(), last_error = NULL "
                    "WHERE entitlement_id = %s RETURNING *", ("APPLIED" if apply else "REMOVED", e["entitlement_id"]))
        audit.record(cur, ACTOR, "ENTITLEMENT_APPLIED" if apply else "ENTITLEMENT_REVOKED", "GROUP_ENTITLEMENT",
                     e["entitlement_id"], after=cur.fetchone(), reason="dry run" if p.dry_run else None)
        _complete(cur, action)


HANDLERS: dict[str, Callable[[dict, Providers], None]] = {
    "CREATE_GROUP": _create_group,
    "ADD_MEMBER": _add_member,
    "REMOVE_MEMBER": _remove_member,
    "APPLY_ENTITLEMENT": lambda a, p: _entitlement(a, p, True),
    "REVOKE_ENTITLEMENT": lambda a, p: _entitlement(a, p, False),
}


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------
def _defer(action: dict, reason: str) -> None:
    with transaction() as cur:
        cur.execute("UPDATE provisioning_action SET status = 'QUEUED', attempts = greatest(attempts - 1, 0), "
                    "next_attempt_at = now() + %s, last_error = %s WHERE action_id = %s",
                    (timedelta(minutes=DEFER_MINUTES), f"deferred: {reason}", action["action_id"]))


def _fail(action: dict, exc: Exception) -> None:
    permanent = isinstance(exc, PermanentError)
    dead = permanent or action["attempts"] >= MAX_ATTEMPTS
    msg = f"{type(exc).__name__}: {exc}"[:2000]
    with transaction() as cur:
        cur.execute("UPDATE provisioning_action SET status = %s, next_attempt_at = now() + %s, last_error = %s "
                    "WHERE action_id = %s",
                    ("DEAD" if dead else "FAILED", backoff(action["attempts"]), msg, action["action_id"]))
        if not dead:
            return
        if action["grant_id"] and action["action_type"] == "ADD_MEMBER":
            cur.execute("UPDATE access_grant SET status = 'FAILED' WHERE grant_id = %s AND status = 'PENDING_PROVISION' "
                        "RETURNING request_id", (action["grant_id"],))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE access_request SET status = 'PROVISIONING_FAILED', version = version + 1 "
                            "WHERE request_id = %s AND status IN ('APPROVED','PROVISIONING')", (row["request_id"],))
        if action["entitlement_id"]:
            cur.execute("UPDATE group_entitlement SET apply_status = 'FAILED', last_error = %s "
                        "WHERE entitlement_id = %s", (msg, action["entitlement_id"]))
        audit.record(cur, ACTOR, "ACTION_DEAD_LETTERED", "PROVISIONING_ACTION", action["action_id"],
                     after={**action, "last_error": msg})
        notify.admin_alert(cur, f"Provisioning failed: {action['action_type']}",
                           f"Action {action['action_id']} failed permanently after {action['attempts']} attempt(s).\n"
                           f"{msg}\n\nFix the cause, then retry it from Operations.", "ACTION_DEAD",
                           dedupe_key=f"dead:{action['action_id']}:{action['attempts']}")


def run(providers: Providers, limit: int = 300) -> dict[str, int]:
    with transaction() as cur:
        stale = reap_stale(cur)
        planned = plan(cur)
    log.info("Reaped %s stale actions; planned %s", stale, planned)
    stats = {"succeeded": 0, "deferred": 0, "failed": 0}
    for action in claim(limit):
        try:
            HANDLERS[action["action_type"]](action, providers)
            stats["succeeded"] += 1
        except DeferError as exc:
            _defer(action, str(exc))
            stats["deferred"] += 1
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the action
            log.exception("Action %s failed", action["action_id"])
            _fail(action, exc)
            stats["failed"] += 1
    log.info("Provisioning run: %s", stats)
    return stats
