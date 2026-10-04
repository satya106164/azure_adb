"""Notifications via an outbox.

The app and jobs only INSERT into notification_outbox, straight after the change
they describe; the notification job sends them (Graph sendMail / Teams workflow
webhook). Messages carry request metadata only - never data content.
"""
from __future__ import annotations

import logging
import uuid
from typing import Iterable

from . import db
from .config import get_settings

log = logging.getLogger(__name__)


def _footer(admin: bool = False) -> str:
    settings = get_settings()
    url = settings.admin_app_url if admin else settings.app_url
    return f"\n\nOpen the {'Catalog admin' if admin else 'access'} app: {url}" if url else ""


def enqueue(recipients: Iterable[str | None], subject: str, body: str, template: str, *,
            request_id=None, dedupe_key: str | None = None, channel: str = "EMAIL", admin_link: bool = False) -> None:
    to = sorted({r.strip().lower() for r in recipients if r and "@" in r})
    if channel == "EMAIL" and not to:
        log.info("No recipients for %s; skipped", template)
        return
    env = get_settings().env
    prefix = "" if env == "prd" else f"[{env.upper()}] "
    db.execute(
        """
        INSERT INTO notification_outbox (notification_id, channel, recipients, subject, body, template, dedupe_key,
                                         request_id, status, attempts, created_at)
        SELECT :id, :channel, :to, :subject, :body, :template, :dedupe, :request_id, 'QUEUED', 0, current_timestamp()
        FROM (SELECT 1) one
        WHERE :dedupe IS NULL OR NOT EXISTS (SELECT 1 FROM notification_outbox WHERE dedupe_key = :dedupe)
        """,
        {"id": str(uuid.uuid4()), "channel": channel, "to": ",".join(to), "subject": prefix + subject,
         "body": body + _footer(admin_link), "template": template, "dedupe": dedupe_key,
         "request_id": None if request_id is None else str(request_id)},
    )


def security_alert(subject: str, body: str, template: str, *, request_id=None, dedupe_key=None) -> None:
    """Email the security mailbox and post to the security Teams channel."""
    settings = get_settings()
    enqueue([settings.security_mailbox], subject, body, template, request_id=request_id,
            dedupe_key=f"{dedupe_key}:mail" if dedupe_key else None)
    enqueue([], subject, body, template, request_id=request_id, channel="TEAMS",
            dedupe_key=f"{dedupe_key}:teams" if dedupe_key else None)


def admin_alert(subject: str, body: str, template: str, *, dedupe_key=None) -> None:
    enqueue([get_settings().admin_mailbox], subject, body, template, dedupe_key=dedupe_key, admin_link=True)
