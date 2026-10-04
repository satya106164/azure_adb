"""People a request can be raised for: the workspace's users (read through Databricks SCIM as the
App's service principal), plus anyone the app already knows. Read-only; nothing is changed."""
from __future__ import annotations

import logging

from .db import fetch_all

log = logging.getLogger(__name__)


def people() -> list[dict]:
    """Active people as [{email, display_name}], sorted by name. Falls back to app_user if SCIM is unavailable."""
    found: dict[str, str] = {}
    try:
        from databricks.sdk import WorkspaceClient

        for u in WorkspaceClient().users.list(attributes="userName,displayName,active"):
            email = (u.user_name or "").strip().lower()
            if "@" in email and u.active is not False:
                found[email] = u.display_name or email
    except Exception as exc:  # the form still offers "someone not in the list"
        log.warning("Could not list workspace users: %s", exc)
    for r in fetch_all("SELECT email, display_name FROM app_user WHERE is_active"):
        found.setdefault(r["email"], r["display_name"] or r["email"])
    return sorted(({"email": e, "display_name": n} for e, n in found.items()),
                  key=lambda p: (p["display_name"].lower(), p["email"]))
