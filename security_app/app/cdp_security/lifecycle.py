"""Daily lifecycle: expiry, expiry notices, SLA escalation, joiners/movers/leavers,
retired-group clean-up. Each function is idempotent and safe to re-run.

Nothing here changes a group: access that has to go is listed for an admin to
remove by hand (Fulfilment page in the Catalog admin app)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import audit, db, notify, service, workflow
from .config import get_settings
from .db import execute, fetch_all, fetch_one
from .models import CurrentUser, RevokeReason

log = logging.getLogger(__name__)
ACTOR = CurrentUser.system("lifecycle")
NOTICE_DAYS = {1: 14, 2: 3}  # notice level -> days before expiry


def expire_grants(now: datetime | None = None) -> int:
    now = now or db.now()
    done = 0
    for g in fetch_all("SELECT * FROM access_grant WHERE status = 'ACTIVE' AND valid_to <= :now", {"now": now}):
        done += service.mark_for_revoke(g, RevokeReason.EXPIRED, ACTOR) is not None
    return done


def send_expiry_notices(now: datetime | None = None) -> int:
    now = now or db.now()
    sent = 0
    for level, days in NOTICE_DAYS.items():
        for r in fetch_all("SELECT * FROM v_grant WHERE status = 'ACTIVE' "
                           "AND expiry_notice_level < :level AND valid_to <= :limit",
                           {"level": level, "limit": now + timedelta(days=days)}):
            if execute("UPDATE access_grant SET expiry_notice_level = :level WHERE grant_id = :id "
                       "AND expiry_notice_level < :level", {"level": level, "id": r["grant_id"]}) != 1:
                continue
            notify.enqueue([r["user_email"]], f"Access ends {r['valid_to']:%d %b %Y}: {r['group_name']}",
                           f"Your read access to {r['data_product_name']} ends on {r['valid_to']:%d %b %Y}. "
                           "If you still need it, request an extension from My access.",
                           "EXPIRY_NOTICE", request_id=r["request_id"], dedupe_key=f"expiry:{r['grant_id']}:{level}")
            sent += 1
    return sent


def escalate_overdue_tasks(now: datetime | None = None) -> dict[str, int]:
    """First breach: escalate to the approvers and admins. Past auto_reject_after_hours: close it."""
    now = now or db.now()
    settings = get_settings()
    out = {"escalated": 0, "timed_out": 0}
    for t in fetch_all("SELECT * FROM v_task WHERE task_status = 'PENDING' AND due_at < :now", {"now": now}):
        overdue_hours = (now - t["due_at"]).total_seconds() / 3600
        if overdue_hours >= settings.auto_reject_after_hours:
            if execute("UPDATE approval_task SET status = 'EXPIRED', decided_at = current_timestamp(), "
                       "decision_comment = 'Not decided within SLA' WHERE task_id = :id AND status = 'PENDING'",
                       {"id": t["task_id"]}) != 1:
                continue
            raw = fetch_one("SELECT status FROM access_request WHERE request_id = :id", {"id": t["request_id"]})
            status = workflow.next_request_status(raw["status"], "timeout", "system")
            execute("UPDATE access_request SET status = :s, decided_at = current_timestamp(), version = version + 1, "
                    "updated_at = current_timestamp() WHERE request_id = :id AND status = :old",
                    {"s": status, "id": t["request_id"], "old": raw["status"]})
            audit.record(ACTOR, "REQUEST_TIMED_OUT", "ACCESS_REQUEST", t["request_id"], correlation_id=t["request_ref"])
            notify.enqueue([t["requester_email"], t["beneficiary_email"]],
                           f"Request closed without a decision: {t['request_ref']}",
                           "No approver decided in time, so the request was closed. Raise it again if still needed.",
                           "REQUEST_TIMED_OUT", request_id=t["request_id"])
            out["timed_out"] += 1
        elif t["escalated_at"] is None:
            if execute("UPDATE approval_task SET escalated_at = current_timestamp() WHERE task_id = :id "
                       "AND escalated_at IS NULL", {"id": t["task_id"]}) != 1:
                continue
            audit.record(ACTOR, "TASK_ESCALATED", "APPROVAL_TASK", t["task_id"], correlation_id=t["request_ref"])
            ctx = service.approval_context(t, service.get_group(t["group_code"]))
            recipients = list(workflow.eligible_approvers(t["approver_role"], ctx))
            subject = f"Overdue approval: {t['request_ref']}"
            body = (f"The approval for {t['beneficiary_email']} ({t['group_name']}) was due "
                    f"{t['due_at']:%d %b %Y %H:%M} UTC. An admin can decide it on the Approvals page of "
                    "Catalog admin if neither approver can.")
            notify.enqueue(recipients + [settings.admin_mailbox], subject, body, "TASK_ESCALATED",
                           request_id=t["request_id"], dedupe_key=f"escalate:{t['task_id']}")
            out["escalated"] += 1
    return out


def process_leavers_and_movers(graph, now: datetime | None = None) -> dict[str, int]:
    """Leavers (disabled/deleted in Entra) lose everything; movers (department change)
    get their access reviewed in a MOVER campaign. Joiners get nothing automatically."""
    from . import recert

    out = {"leavers": 0, "movers": 0}
    users = fetch_all("SELECT DISTINCT u.* FROM app_user u JOIN access_grant g ON g.user_email = u.email "
                      "WHERE u.is_active AND g.status IN ('ACTIVE', 'PENDING_PROVISION')")
    for u in users:
        entra = graph.resolve_user(u["email"])
        if entra is None or not entra.get("accountEnabled", True):
            execute("UPDATE app_user SET is_active = false, deactivated_at = current_timestamp(), "
                    "updated_at = current_timestamp() WHERE email = :e", {"e": u["email"]})
            audit.record(ACTOR, "LEAVER_PROCESSED", "APP_USER", u["email"], before=u)
            for g in fetch_all("SELECT * FROM access_grant WHERE user_email = :e AND status = 'ACTIVE'",
                               {"e": u["email"]}):
                service.mark_for_revoke(g, RevokeReason.LEAVER, ACTOR)
            for g in fetch_all("SELECT * FROM access_grant WHERE user_email = :e AND status = 'PENDING_PROVISION'",
                               {"e": u["email"]}):
                service.withdraw_pending_grant(g, RevokeReason.LEAVER, ACTOR, "leaver")
            open_ids = [r["request_id"] for r in fetch_all(
                "SELECT request_id FROM access_request WHERE beneficiary_email = :e "
                "AND status IN ('DRAFT', 'PENDING_APPROVAL')", {"e": u["email"]})]
            if open_ids:
                execute("UPDATE approval_task SET status = 'CANCELLED' WHERE status = 'PENDING' "
                        "AND request_id IN (:ids)", {"ids": open_ids})
                execute("UPDATE access_request SET status = 'CANCELLED', decided_at = current_timestamp(), "
                        "version = version + 1, updated_at = current_timestamp() WHERE request_id IN (:ids) "
                        "AND status IN ('DRAFT', 'PENDING_APPROVAL')", {"ids": open_ids})
            owned = fetch_all("SELECT data_product_code, name FROM data_product WHERE owner_email = :e "
                              "OR approver2_email = :e", {"e": u["email"]})
            if owned:
                notify.admin_alert(f"Leaver approves {len(owned)} data product(s): {u['email']}",
                                   "Set a new owner / approver or approver 2 in Catalog admin: "
                                   + ", ".join(f"{d['data_product_code']} {d['name']}" for d in owned),
                                   "LEAVER_OWNER", dedupe_key=f"leaver-owner:{u['email']}")
            out["leavers"] += 1
            continue
        dept = entra.get("department")
        if u["department"] and dept and dept != u["department"]:
            audit.record(ACTOR, "MOVER_DETECTED", "APP_USER", u["email"],
                         before={"department": u["department"]}, after={"department": dept})
            recert.add_mover_review(u["email"], now)
            out["movers"] += 1
        execute("UPDATE app_user SET department = :d, entra_object_id = :oid, "
                "display_name = coalesce(:name, display_name), updated_at = current_timestamp() WHERE email = :e",
                {"d": dept, "oid": entra.get("id"), "name": entra.get("displayName"), "e": u["email"]})
    return out


def finalise_retired_groups() -> int:
    """RETIRING -> RETIRED once no member and no grant remains. Access that was granted after the
    group started retiring is listed for removal here too."""
    done = 0
    for g in fetch_all("SELECT * FROM access_group WHERE status = 'RETIRING'"):
        for grant in fetch_all("SELECT * FROM access_grant WHERE group_code = :g AND status = 'ACTIVE'",
                               {"g": g["group_code"]}):
            service.mark_for_revoke(grant, RevokeReason.GROUP_RETIRED, ACTOR, "group retired")
        done += service.retire_if_empty(ACTOR, g["group_code"])
    return done
