"""Notifications via an outbox.

The app and jobs only INSERT into notification_outbox, straight after the change
they describe; the notification job hands them to the Logic App, which emails them
(dispatch.py). Messages carry request metadata only - never data content.
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
            request_id=None, dedupe_key: str | None = None, admin_link: bool = False) -> None:
    to = sorted({r.strip().lower() for r in recipients if r and "@" in r})
    if not to:
        log.info("No recipients for %s; skipped", template)
        return
    env = get_settings().env
    prefix = "" if env == "prd" else f"[{env.upper()}] "
    db.execute(
        """
        INSERT INTO notification_outbox (notification_id, channel, recipients, subject, body, template, dedupe_key,
                                         request_id, status, attempts, created_at)
        SELECT :id, 'EMAIL', :to, :subject, :body, :template, :dedupe, :request_id, 'QUEUED', 0, current_timestamp()
        FROM (SELECT 1) one
        WHERE :dedupe IS NULL OR NOT EXISTS (SELECT 1 FROM notification_outbox WHERE dedupe_key = :dedupe)
        """,
        {"id": str(uuid.uuid4()), "to": ",".join(to), "subject": prefix + subject,
         "body": body + _footer(admin_link), "template": template, "dedupe": dedupe_key,
         "request_id": None if request_id is None else str(request_id)},
    )


def admin_alert(subject: str, body: str, template: str, *, dedupe_key=None) -> None:
    enqueue([get_settings().admin_mailbox], subject, body, template, dedupe_key=dedupe_key, admin_link=True)
