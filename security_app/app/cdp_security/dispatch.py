"""Send queued notifications (run by jobs only)."""
from __future__ import annotations

import logging

import requests

from .db import transaction

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
    with transaction() as cur:
        cur.execute("""
            UPDATE notification_outbox SET attempts = attempts + 1
            WHERE notification_id IN (SELECT notification_id FROM notification_outbox
                                      WHERE status IN ('QUEUED','FAILED') ORDER BY created_at
                                      FOR UPDATE SKIP LOCKED LIMIT %s)
            RETURNING *""", (limit,))
        batch = cur.fetchall()
    for n in batch:
        try:
            if dry_run:
                log.info("DRY RUN %s to %s: %s", n["channel"], n["recipients"], n["subject"])
            elif n["channel"] == "EMAIL":
                if not (graph and sender_mailbox):
                    raise RuntimeError("Email sender not configured")
                graph.send_mail(sender_mailbox, list(n["recipients"]), n["subject"], n["body"])
            else:
                if not teams:
                    raise RuntimeError("Teams webhook not configured")
                teams.send(n["subject"], n["body"])
            status, err = "SENT", None
            stats["sent"] += 1
        except Exception as exc:  # noqa: BLE001
            status = "DEAD" if n["attempts"] >= MAX_ATTEMPTS else "FAILED"
            err = str(exc)[:1000]
            stats["failed"] += 1
            log.warning("Notification %s failed: %s", n["notification_id"], err)
        with transaction() as cur:
            cur.execute("UPDATE notification_outbox SET status = %s, last_error = %s, "
                        "sent_at = CASE WHEN %s = 'SENT' THEN now() END WHERE notification_id = %s",
                        (status, err, status, n["notification_id"]))
    return stats
