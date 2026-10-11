"""Send queued notifications (run by jobs only, one run at a time).

Each queued email is posted as JSON to an Azure Logic App HTTP trigger, which sends it
(e.g. Office 365 Outlook). The payload is built by `payload()`; docs/logic_app/ has a
reference workflow. The trigger URL carries a signature, so it lives in the secret scope.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from .config import get_settings
from .db import execute, fetch_all

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5

# Emails for admins link to the Catalog admin app; the rest to a page of the Data access app
ADMIN_TEMPLATES = frozenset({"ADD_MEMBER", "REMOVE_MEMBER", "NO_APPROVER", "LEAVER_OWNER", "RECERT_NO_REVIEWER"})
APPROVER_TEMPLATES = frozenset({"APPROVAL_NEEDED", "TASK_ESCALATED"})


class LogicAppSender:
    """Posts one email to a Logic App 'When a HTTP request is received' trigger."""

    def __init__(self, url: str, timeout: int = 30):
        self.url = url
        self.timeout = timeout

    def send(self, payload: dict) -> None:
        import requests

        resp = requests.post(self.url, json=payload, timeout=self.timeout)
        if resp.status_code >= 300:
            raise RuntimeError(f"Logic App returned {resp.status_code}: {resp.text[:200]}")


def link(template: str, request_ref: str | None, app_url: str | None, admin_app_url: str | None) -> str | None:
    """Where the email's button goes: the approval itself for approvers, Catalog admin for admins."""
    base = (admin_app_url if template in ADMIN_TEMPLATES else app_url or "").rstrip("/")
    if not base:
        return None
    if template in APPROVER_TEMPLATES:
        return f"{base}/approvals" + (f"?request={quote(request_ref)}" if request_ref else "")
    if template == "RECERT_REVIEW":
        return f"{base}/recertification"
    return base


def payload(n: dict, app_url: str | None, admin_app_url: str | None) -> dict:
    """The JSON the Logic App receives for one notification_outbox row (with request_ref joined in)."""
    return {
        "notification_id": n["notification_id"],
        "to": [r for r in (n["recipients"] or "").split(",") if r],
        "subject": n["subject"],
        "body": n["body"],
        "link": link(n["template"], n.get("request_ref"), app_url, admin_app_url),
        "template": n["template"],
        "request_ref": n.get("request_ref"),
    }


def dispatch(sender: LogicAppSender | None = None, dry_run: bool = True, limit: int = 200) -> dict[str, int]:
    settings = get_settings()
    stats = {"sent": 0, "failed": 0}
    queued = fetch_all("SELECT n.*, r.request_ref FROM notification_outbox n "
                       "LEFT JOIN access_request r ON r.request_id = n.request_id "
                       f"WHERE n.status IN ('QUEUED', 'FAILED') ORDER BY n.created_at LIMIT {int(limit)}")
    for n in queued:
        # Claim: count the attempt only if nobody else did meanwhile
        if execute("UPDATE notification_outbox SET attempts = attempts + 1 WHERE notification_id = :id "
                   "AND attempts = :a AND status IN ('QUEUED', 'FAILED')",
                   {"id": n["notification_id"], "a": n["attempts"]}) != 1:
            continue
        attempts = n["attempts"] + 1
        body = payload(n, settings.app_url, settings.admin_app_url)
        try:
            if dry_run:
                log.info("DRY RUN email to %s: %s (%s)", body["to"], body["subject"], body["link"])
            else:
                if sender is None:
                    raise RuntimeError("Logic App URL not configured (secret logic-app-email-url)")
                sender.send(body)
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
