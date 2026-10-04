"""Recertification campaigns. Periodic campaigns are monthly buckets; each grant
enters one when it is within `recert_window_days` of its interval. Items not
attested by the campaign due date are revoked (fail closed)."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from . import audit, db, notify, service, workflow
from .config import get_settings
from .db import execute, fetch_all, fetch_one
from .models import ApproverRole, CurrentUser, RevokeReason

log = logging.getLogger(__name__)
ACTOR = CurrentUser.system("recertification")


def _campaign(ctype: str, now: datetime, due_days: int) -> dict:
    """Reuse an open campaign of this type that still leaves reviewers >= 3 days; else open a new one."""
    camp = fetch_one("SELECT * FROM recert_campaign WHERE campaign_type = :t AND status = 'OPEN' AND due_at > :cut "
                     "ORDER BY opened_at DESC LIMIT 1", {"t": ctype, "cut": now + timedelta(days=3)})
    if camp:
        return camp
    label = {"PERIODIC": "Periodic review", "MOVER": "Mover review"}.get(ctype, "Review")
    camp = {"campaign_id": str(uuid.uuid4()), "name": f"{label} {now:%Y-%m-%d %H%M}", "campaign_type": ctype,
            "opened_at": now, "due_at": now + timedelta(days=due_days), "status": "OPEN", "created_by": ACTOR.email}
    execute("INSERT INTO recert_campaign (campaign_id, name, campaign_type, opened_at, due_at, status, created_by) "
            "VALUES (:campaign_id, :name, :campaign_type, :opened_at, :due_at, :status, :created_by)", camp)
    return camp


def _add_item(campaign: dict, grant: dict) -> bool:
    holders = service.owners_for_group(grant)
    reviewer = workflow.choose_reviewer(grant["user_email"], holders.get(ApproverRole.DATA_OWNER, set()),
                                        holders.get(ApproverRole.STEWARD, set()))
    if reviewer is None:
        notify.admin_alert(f"No reviewer for {grant['group_name']}",
                           f"Grant for {grant['user_email']} cannot be recertified: the data product has no active "
                           "owner or steward other than the grantee. Set one in Catalog admin.", "RECERT_NO_REVIEWER",
                           dedupe_key=f"noreviewer:{grant['grant_id']}:{campaign['campaign_id']}")
        return False
    service.ensure_user(reviewer)
    return execute(
        "INSERT INTO recert_item (item_id, campaign_id, grant_id, reviewer_email, decision, created_at) "
        "SELECT :id, :c, :g, :r, 'PENDING', current_timestamp() FROM (SELECT 1) one "
        "WHERE NOT EXISTS (SELECT 1 FROM recert_item WHERE grant_id = :g AND (decision = 'PENDING' OR campaign_id = :c))",
        {"id": str(uuid.uuid4()), "c": campaign["campaign_id"], "g": grant["grant_id"], "r": reviewer}) == 1


def open_periodic(now: datetime | None = None, due_days: int = 14) -> int:
    now = now or db.now()
    settings = get_settings()
    candidates = fetch_all("""
        SELECT v.*, c.recert_interval_days,
               coalesce(v.last_recertified_at, v.provisioned_at, v.valid_from) AS last_attested
        FROM v_grant v JOIN classification c ON c.classification_code = v.classification_code
        WHERE v.status = 'ACTIVE' AND NOT v.is_break_glass
          AND NOT EXISTS (SELECT 1 FROM recert_item i WHERE i.grant_id = v.grant_id AND i.decision = 'PENDING')""")
    due = [g for g in candidates
           if workflow.recert_due(g["last_attested"], g["recert_interval_days"], settings.recert_window_days, now)]
    if not due:
        return 0
    campaign = _campaign("PERIODIC", now, due_days)
    added = sum(_add_item(campaign, g) for g in due)
    if added:
        audit.record(ACTOR, "RECERT_ITEMS_ADDED", "RECERT_CAMPAIGN", campaign["campaign_id"], after={"added": added})
    _notify_reviewers(campaign)
    return added


def add_mover_review(user_email: str, now: datetime | None = None) -> int:
    now = now or db.now()
    grants = fetch_all("SELECT * FROM v_grant v WHERE v.user_email = :e AND v.status = 'ACTIVE' AND NOT v.is_break_glass "
                       "AND NOT EXISTS (SELECT 1 FROM recert_item i WHERE i.grant_id = v.grant_id "
                       "AND i.decision = 'PENDING')", {"e": user_email})
    if not grants:
        return 0
    campaign = _campaign("MOVER", now, 7)
    added = sum(_add_item(campaign, g) for g in grants)
    _notify_reviewers(campaign)
    return added


def _notify_reviewers(campaign: dict) -> None:
    for r in fetch_all("SELECT reviewer_email, count(*) AS n FROM recert_item WHERE campaign_id = :c "
                       "AND decision = 'PENDING' GROUP BY reviewer_email", {"c": campaign["campaign_id"]}):
        notify.enqueue([r["reviewer_email"]], f"Access review: {r['n']} item(s) due {campaign['due_at']:%d %b %Y}",
                       f"You have {r['n']} access grant(s) to review in '{campaign['name']}'. "
                       "Anything not reviewed by the due date is listed for removal.",
                       "RECERT_REVIEW", dedupe_key=f"recert:{campaign['campaign_id']}:{r['reviewer_email']}:{r['n']}")


def close_overdue(now: datetime | None = None) -> int:
    now = now or db.now()
    revoked = 0
    for camp in fetch_all("SELECT * FROM recert_campaign WHERE status = 'OPEN' AND due_at < :now", {"now": now}):
        items = fetch_all("SELECT * FROM recert_item WHERE campaign_id = :c AND decision = 'PENDING'",
                          {"c": camp["campaign_id"]})
        for item in items:
            if execute("UPDATE recert_item SET decision = 'NOT_ATTESTED', decided_by = :by, "
                       "decided_at = current_timestamp() WHERE item_id = :id AND decision = 'PENDING'",
                       {"by": ACTOR.email, "id": item["item_id"]}) != 1:
                continue
            g = fetch_one("SELECT * FROM access_grant WHERE grant_id = :id", {"id": item["grant_id"]})
            if g["status"] == "ACTIVE" and service.mark_for_revoke(g, RevokeReason.RECERT_NOT_ATTESTED, ACTOR):
                revoked += 1
        execute("UPDATE recert_campaign SET status = 'CLOSED', closed_at = current_timestamp() "
                "WHERE campaign_id = :c AND status = 'OPEN'", {"c": camp["campaign_id"]})
        audit.record(ACTOR, "RECERT_CAMPAIGN_CLOSED", "RECERT_CAMPAIGN", camp["campaign_id"],
                     after={"not_attested": len(items)})
    return revoked
