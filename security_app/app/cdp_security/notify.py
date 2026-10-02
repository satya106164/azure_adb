"""Notifications via a transactional outbox.

The app and jobs only INSERT into notification_outbox inside their own
transaction; the notification job sends them (Graph sendMail / Teams workflow
webhook). Messages carry request metadata only - never data content.
"""
from __future__ import annotations

import logging
from typing import Iterable

from .config import get_settings

log = logging.getLogger(__name__)


def _footer() -> str:
    url = get_settings().app_url
    return f"\n\nOpen the access app: {url}" if url else ""


def enqueue(cur, recipients: Iterable[str | None], subject: str, body: str, template: str, *,
            request_id=None, dedupe_key: str | None = None, channel: str = "EMAIL") -> None:
    to = sorted({r.strip().lower() for r in recipients if r and "@" in r})
    if channel == "EMAIL" and not to:
        log.info("No recipients for %s; skipped", template)
        return
    env = get_settings().env
    prefix = "" if env == "prd" else f"[{env.upper()}] "
    cur.execute(
        """
        INSERT INTO notification_outbox (channel, recipients, subject, body, template, request_id, dedupe_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (dedupe_key) DO NOTHING
        """,
        (channel, to, prefix + subject, body + _footer(), template, request_id, dedupe_key),
    )


def security_alert(cur, subject: str, body: str, template: str, *, request_id=None, dedupe_key=None) -> None:
    """Email the security mailbox and post to the security Teams channel."""
    settings = get_settings()
    enqueue(cur, [settings.security_mailbox], subject, body, template, request_id=request_id,
            dedupe_key=f"{dedupe_key}:mail" if dedupe_key else None)
    enqueue(cur, [], subject, body, template, request_id=request_id, channel="TEAMS",
            dedupe_key=f"{dedupe_key}:teams" if dedupe_key else None)


def admin_alert(cur, subject: str, body: str, template: str, *, dedupe_key=None) -> None:
    enqueue(cur, [get_settings().admin_mailbox], subject, body, template, dedupe_key=dedupe_key)
