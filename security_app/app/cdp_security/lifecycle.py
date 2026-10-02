"""Daily lifecycle: expiry, expiry notices, SLA escalation, joiners/movers/leavers,
retired-group clean-up. Each function is idempotent and safe to re-run."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import audit, notify, service, workflow
from .config import get_settings
from .db import transaction
from .models import CurrentUser, RevokeReason

log = logging.getLogger(__name__)
ACTOR = CurrentUser.system("lifecycle")
NOTICE_DAYS = {1: 14, 2: 3}  # notice level -> days before expiry


def expire_grants(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    with transaction() as cur:
        cur.execute("SELECT * FROM access_grant WHERE status = 'ACTIVE' AND valid_to <= %s FOR UPDATE SKIP LOCKED",
                    (now,))
        rows = cur.fetchall()
        for g in rows:
            service.mark_for_revoke(cur, g, RevokeReason.EXPIRED, ACTOR)
    return len(rows)


def send_expiry_notices(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    sent = 0
    with transaction() as cur:
        for level, days in NOTICE_DAYS.items():
            cur.execute(
                "SELECT g.grant_id, g.request_id, g.valid_to, g.is_break_glass, v.user_email, v.group_name "
                "FROM access_grant g JOIN v_grant v ON v.grant_id = g.grant_id "
                "WHERE g.status = 'ACTIVE' AND NOT g.is_break_glass AND g.expiry_notice_level < %s "
                "AND g.valid_to <= %s FOR UPDATE OF g SKIP LOCKED", (level, now + timedelta(days=days)))
            for r in cur.fetchall():
                notify.enqueue(cur, [r["user_email"]], f"Access ends {r['valid_to']:%d %b %Y}: {r['group_name']}",
                               f"Your access to {r['group_name']} ends on {r['valid_to']:%d %b %Y}. "
                               "If you still need it, request an extension from My access.",
                               "EXPIRY_NOTICE", request_id=r["request_id"], dedupe_key=f"expiry:{r['grant_id']}:{level}")
                cur.execute("UPDATE access_grant SET expiry_notice_level = %s WHERE grant_id = %s",
                            (level, r["grant_id"]))
                sent += 1
    return sent


def escalate_overdue_tasks(now: datetime | None = None) -> dict[str, int]:
    """First breach: escalate. Past auto_reject_after_hours: reject (never for break-glass reviews)."""
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    out = {"escalated": 0, "timed_out": 0}
    with transaction() as cur:
        cur.execute("SELECT t.*, r.request_ref FROM approval_task t JOIN access_request r USING (request_id) "
                    "WHERE t.status = 'PENDING' AND t.due_at < %s FOR UPDATE OF t SKIP LOCKED", (now,))
        for t in cur.fetchall():
            req = cur.execute("SELECT * FROM v_request WHERE request_id = %s", (t["request_id"],)).fetchone()
            overdue_hours = (now - t["due_at"]).total_seconds() / 3600
            if not t["is_post_hoc"] and overdue_hours >= settings.auto_reject_after_hours:
                cur.execute("UPDATE approval_task SET status = 'EXPIRED', decided_at = now(), "
                            "decision_comment = 'Not decided within SLA' WHERE task_id = %s", (t["task_id"],))
                raw = cur.execute("SELECT status FROM access_request WHERE request_id = %s FOR UPDATE",
                                  (t["request_id"],)).fetchone()
                status = workflow.next_request_status(raw["status"], "timeout", "system")
                cur.execute("UPDATE access_request SET status = %s, decided_at = now(), version = version + 1 "
                            "WHERE request_id = %s", (status.value, t["request_id"]))
                audit.record(cur, ACTOR, "REQUEST_TIMED_OUT", "ACCESS_REQUEST", t["request_id"],
                             correlation_id=t["request_ref"])
                notify.enqueue(cur, [req["requester_email"], req["beneficiary_email"]],
                               f"Request closed without a decision: {t['request_ref']}",
                               "No approver decided in time, so the request was closed. Raise it again if still needed.",
                               "REQUEST_TIMED_OUT", request_id=t["request_id"])
                out["timed_out"] += 1
            elif t["escalated_at"] is None:
                cur.execute("UPDATE approval_task SET escalated_at = now() WHERE task_id = %s", (t["task_id"],))
                audit.record(cur, ACTOR, "TASK_ESCALATED", "APPROVAL_TASK", t["task_id"], correlation_id=t["request_ref"])
                group = service.get_group(cur, req["access_group_id"])
                ctx = service.approval_context(cur, req, group)
                recipients = list(workflow.eligible_approvers(t["approver_role"], ctx)) \
                    if t["approver_role"] != "SECURITY" else []
                subject = f"Overdue approval: {t['request_ref']}"
                body = f"Stage {t['stage_no']} ({t['approver_role']}) was due {t['due_at']:%d %b %Y %H:%M} UTC."
                notify.enqueue(cur, recipients + [settings.admin_mailbox], subject, body, "TASK_ESCALATED",
                               request_id=t["request_id"], dedupe_key=f"escalate:{t['task_id']}")
                if t["approver_role"] == "SECURITY" or t["is_post_hoc"]:
                    notify.security_alert(cur, subject, body, "TASK_ESCALATED", request_id=t["request_id"],
                                          dedupe_key=f"escalate-sec:{t['task_id']}")
                out["escalated"] += 1
    return out


def process_leavers_and_movers(graph, now: datetime | None = None) -> dict[str, int]:
    """Leavers (disabled/deleted in Entra) lose everything; movers (department change)
    get their access reviewed in a MOVER campaign. Joiners get nothing automatically."""
    from . import recert

    out = {"leavers": 0, "movers": 0}
    with transaction() as cur:
        cur.execute("SELECT DISTINCT u.* FROM app_user u JOIN access_grant g ON g.user_id = u.user_id "
                    "WHERE u.is_active AND g.status IN ('ACTIVE','PENDING_PROVISION')")
        users = cur.fetchall()
    for u in users:
        entra = graph.resolve_user(u["email"])
        with transaction() as cur:
            if entra is None or not entra.get("accountEnabled", True):
                cur.execute("UPDATE app_user SET is_active = false, deactivated_at = now() WHERE user_id = %s",
                            (u["user_id"],))
                for g in cur.execute("SELECT * FROM access_grant WHERE user_id = %s AND status = 'ACTIVE' FOR UPDATE",
                                     (u["user_id"],)).fetchall():
                    service.mark_for_revoke(cur, g, RevokeReason.LEAVER, ACTOR)
                cur.execute("UPDATE approval_task SET status = 'CANCELLED' WHERE status = 'PENDING' AND request_id IN "
                            "(SELECT request_id FROM access_request WHERE beneficiary_user_id = %s "
                            "AND status = 'PENDING_APPROVAL')", (u["user_id"],))
                cur.execute("UPDATE access_request SET status = 'CANCELLED', decided_at = now() "
                            "WHERE beneficiary_user_id = %s AND status IN ('DRAFT','PENDING_APPROVAL')", (u["user_id"],))
                audit.record(cur, ACTOR, "LEAVER_PROCESSED", "APP_USER", u["user_id"], before=u)
                owned = cur.execute("SELECT count(*) AS n FROM ownership WHERE user_id = %s AND valid_to IS NULL",
                                    (u["user_id"],)).fetchone()["n"]
                if owned:
                    notify.admin_alert(cur, f"Leaver owns {owned} governed object(s): {u['email']}",
                                       "Reassign their data owner / steward roles in Catalog admin.", "LEAVER_OWNER",
                                       dedupe_key=f"leaver-owner:{u['user_id']}")
                out["leavers"] += 1
                continue
            dept = entra.get("department")
            if u["department"] and dept and dept != u["department"]:
                audit.record(cur, ACTOR, "MOVER_DETECTED", "APP_USER", u["user_id"],
                             before={"department": u["department"]}, after={"department": dept})
                recert.add_mover_review(cur, u["user_id"], now)
                out["movers"] += 1
            cur.execute("UPDATE app_user SET department = %s, entra_object_id = %s, display_name = coalesce(%s, display_name) "
                        "WHERE user_id = %s", (dept, entra.get("id"), entra.get("displayName"), u["user_id"]))
    return out


def finalise_retired_groups() -> int:
    with transaction() as cur:
        cur.execute("""
            UPDATE access_group g SET status = 'RETIRED'
            WHERE g.status = 'RETIRING'
              AND NOT EXISTS (SELECT 1 FROM access_grant a WHERE a.access_group_id = g.access_group_id
                              AND a.status IN ('PENDING_PROVISION','ACTIVE','PENDING_REVOKE'))
              AND NOT EXISTS (SELECT 1 FROM group_entitlement e WHERE e.access_group_id = g.access_group_id
                              AND (e.desired_state = 'PRESENT' OR e.apply_status = 'PENDING'))
            RETURNING access_group_id""")
        rows = cur.fetchall()
        for r in rows:
            audit.record(cur, ACTOR, "GROUP_RETIRED", "ACCESS_GROUP", r["access_group_id"])
    return len(rows)
