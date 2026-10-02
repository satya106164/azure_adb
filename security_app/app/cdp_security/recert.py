"""Recertification campaigns. Periodic campaigns are monthly buckets; each grant
enters one when it is within `recert_window_days` of its interval. Items not
attested by the campaign due date are revoked (fail closed)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import audit, notify, service, workflow
from .config import get_settings
from .db import transaction
from .models import ApproverRole, CurrentUser, RevokeReason

log = logging.getLogger(__name__)
ACTOR = CurrentUser.system("recertification")


def _campaign(cur, ctype: str, now: datetime, due_days: int) -> dict:
    """Reuse an open campaign of this type that still leaves reviewers >= 3 days; else open a new one."""
    cur.execute("SELECT * FROM recert_campaign WHERE campaign_type = %s AND status = 'OPEN' AND due_at > %s "
                "ORDER BY opened_at DESC LIMIT 1", (ctype, now + timedelta(days=3)))
    camp = cur.fetchone()
    if camp:
        return camp
    label = {"PERIODIC": "Periodic review", "MOVER": "Mover review"}.get(ctype, "Review")
    cur.execute("INSERT INTO recert_campaign (name, campaign_type, due_at, created_by) VALUES (%s, %s, %s, %s) "
                "RETURNING *", (f"{label} {now:%Y-%m-%d %H%M}", ctype, now + timedelta(days=due_days), ACTOR.email))
    return cur.fetchone()


def _reviewer_for(cur, grant: dict) -> str | None:
    group = service.get_group(cur, grant["access_group_id"])
    holders = service.owners_for_group(cur, group)
    return workflow.choose_reviewer(grant["user_email"], holders.get(ApproverRole.DATA_OWNER, set()),
                                    holders.get(ApproverRole.STEWARD, set()), group.get("owner_email"))


def _add_item(cur, campaign: dict, grant: dict) -> bool:
    reviewer = _reviewer_for(cur, grant)
    if reviewer is None:
        notify.admin_alert(cur, f"No reviewer for {grant['group_name']}",
                           f"Grant for {grant['user_email']} cannot be recertified: no owner/steward other than "
                           "the grantee. Assign one in Catalog admin.", "RECERT_NO_REVIEWER",
                           dedupe_key=f"noreviewer:{grant['grant_id']}:{campaign['campaign_id']}")
        return False
    reviewer_id = service.ensure_user(cur, reviewer)["user_id"]
    cur.execute("INSERT INTO recert_item (campaign_id, grant_id, reviewer_user_id) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING item_id", (campaign["campaign_id"], grant["grant_id"], reviewer_id))
    return cur.fetchone() is not None


def open_periodic(now: datetime | None = None, due_days: int = 14) -> int:
    now = now or datetime.now(timezone.utc)
    settings = get_settings()
    added = 0
    with transaction() as cur:
        campaign = _campaign(cur, "PERIODIC", now, due_days)
        cur.execute("""
            SELECT v.*, c.recert_interval_days,
                   coalesce(g.last_recertified_at, g.provisioned_at, g.valid_from) AS last_attested
            FROM v_grant v JOIN access_grant g ON g.grant_id = v.grant_id
            JOIN classification c ON c.classification_code = v.classification_code
            WHERE v.status = 'ACTIVE' AND NOT v.is_break_glass
              AND NOT EXISTS (SELECT 1 FROM recert_item i WHERE i.grant_id = v.grant_id AND i.decision = 'PENDING')""")
        for g in cur.fetchall():
            if workflow.recert_due(g["last_attested"], g["recert_interval_days"], settings.recert_window_days, now):
                added += _add_item(cur, campaign, g)
        if added:
            audit.record(cur, ACTOR, "RECERT_ITEMS_ADDED", "RECERT_CAMPAIGN", campaign["campaign_id"],
                         after={"added": added})
        _notify_reviewers(cur, campaign)
    return added


def add_mover_review(cur, user_id, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    campaign = _campaign(cur, "MOVER", now, 7)
    cur.execute("SELECT * FROM v_grant WHERE user_id = %s AND status = 'ACTIVE' AND NOT is_break_glass "
                "AND NOT EXISTS (SELECT 1 FROM recert_item i WHERE i.grant_id = v_grant.grant_id "
                "AND i.decision = 'PENDING')", (user_id,))
    added = sum(_add_item(cur, campaign, g) for g in cur.fetchall())
    _notify_reviewers(cur, campaign)
    return added


def _notify_reviewers(cur, campaign: dict) -> None:
    cur.execute("SELECT u.email, count(*) AS n FROM recert_item i JOIN app_user u ON u.user_id = i.reviewer_user_id "
                "WHERE i.campaign_id = %s AND i.decision = 'PENDING' GROUP BY u.email", (campaign["campaign_id"],))
    for r in cur.fetchall():
        notify.enqueue(cur, [r["email"]], f"Access review: {r['n']} item(s) due {campaign['due_at']:%d %b %Y}",
                       f"You have {r['n']} access grant(s) to review in '{campaign['name']}'. "
                       "Anything not reviewed by the due date is removed automatically.",
                       "RECERT_REVIEW", dedupe_key=f"recert:{campaign['campaign_id']}:{r['email']}:{r['n']}")


def close_overdue(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    revoked = 0
    with transaction() as cur:
        cur.execute("SELECT * FROM recert_campaign WHERE status = 'OPEN' AND due_at < %s FOR UPDATE", (now,))
        for camp in cur.fetchall():
            cur.execute("UPDATE recert_item SET decision = 'NOT_ATTESTED', decided_by = %s, decided_at = now() "
                        "WHERE campaign_id = %s AND decision = 'PENDING' RETURNING grant_id",
                        (ACTOR.email, camp["campaign_id"]))
            for item in cur.fetchall():
                g = cur.execute("SELECT * FROM access_grant WHERE grant_id = %s FOR UPDATE",
                                (item["grant_id"],)).fetchone()
                if g["status"] == "ACTIVE":
                    service.mark_for_revoke(cur, g, RevokeReason.RECERT_NOT_ATTESTED, ACTOR)
                    revoked += 1
            cur.execute("UPDATE recert_campaign SET status = 'CLOSED', closed_at = now() WHERE campaign_id = %s",
                        (camp["campaign_id"],))
            audit.record(cur, ACTOR, "RECERT_CAMPAIGN_CLOSED", "RECERT_CAMPAIGN", camp["campaign_id"],
                         after={"revoked_not_attested": revoked})
    return revoked
