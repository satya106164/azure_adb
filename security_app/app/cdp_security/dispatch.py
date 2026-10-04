"""Send queued notifications (run by jobs only, one run at a time)."""
from __future__ import annotations

import logging

import requests

from .db import execute, fetch_all

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


class TeamsWorkflowSender:
    """Teams channel via a Power Automate 'post to channel when a webhook request is received' workflow."""

    def __init__(self, url: str):
        self.url = url

    def send(self, subject: str, body: str) -> None:
        card = {
            "type": "AdaptiveCard", "version": "1.4",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "body": [{"type": "TextBlock", "text": subject, "weight": "Bolder", "wrap": True},
                     {"type": "TextBlock", "text": body, "wrap": True}],
        }
        resp = requests.post(self.url, timeout=20, json={"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]})
        if resp.status_code >= 300:
            raise RuntimeError(f"Teams webhook {resp.status_code}: {resp.text[:200]}")


def dispatch(graph=None, sender_mailbox: str | None = None, teams: TeamsWorkflowSender | None = None,
             dry_run: bool = True, limit: int = 200) -> dict[str, int]:
    stats = {"sent": 0, "failed": 0}
    queued = fetch_all(f"SELECT * FROM notification_outbox WHERE status IN ('QUEUED', 'FAILED') "
                       f"ORDER BY created_at LIMIT {int(limit)}")
    for n in queued:
        # Claim: count the attempt only if nobody else did meanwhile
        if execute("UPDATE notification_outbox SET attempts = attempts + 1 WHERE notification_id = :id "
                   "AND attempts = :a AND status IN ('QUEUED', 'FAILED')",
                   {"id": n["notification_id"], "a": n["attempts"]}) != 1:
            continue
        attempts = n["attempts"] + 1
        recipients = [r for r in (n["recipients"] or "").split(",") if r]
        try:
            if dry_run:
                log.info("DRY RUN %s to %s: %s", n["channel"], recipients, n["subject"])
            elif n["channel"] == "EMAIL":
                if not (graph and sender_mailbox):
                    raise RuntimeError("Email sender not configured")
                graph.send_mail(sender_mailbox, recipients, n["subject"], n["body"])
            else:
                if not teams:
                    raise RuntimeError("Teams webhook not configured")
                teams.send(n["subject"], n["body"])
            status, err = "SENT", None
            stats["sent"] += 1
        except Exception as exc:  # noqa: BLE001
            status = "DEAD" if attempts >= MAX_ATTEMPTS else "FAILED"
            err = str(exc)[:1000]
            stats["failed"] += 1
            log.warning("Notification %s failed: %s", n["notification_id"], err)
        execute("UPDATE notification_outbox SET status = :s, last_error = :err, "
                "sent_at = CASE WHEN :s = 'SENT' THEN current_timestamp() END WHERE notification_id = :id",
                {"s": status, "err": err, "id": n["notification_id"]})
    return stats
