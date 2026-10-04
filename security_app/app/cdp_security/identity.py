"""Who is calling the App, and which global app roles they hold.

Databricks Apps put the signed-in user's identity in X-Forwarded-* headers.
With user authorization enabled (scope iam.current-user:read), the
X-Forwarded-Access-Token header carries an on-behalf-of token that we use only
to read the user's own group memberships. All data changes are made by the
App's service principal and attributed to the user in the audit trail.
App roles come only from Entra group membership (there is no in-app role table).
"""
from __future__ import annotations

import logging

from .config import get_settings
from .models import AppRole, CurrentUser

log = logging.getLogger(__name__)


def _host() -> str:
    import os

    host = os.environ.get("DATABRICKS_HOST", "")
    return host if host.startswith("https://") else f"https://{host}"


def groups_from_token(token: str | None) -> set[str]:
    if not token:
        return set()
    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient(host=_host(), token=token, auth_type="pat")
        me = w.current_user.me()
        return {g.display for g in (me.groups or []) if g.display}
    except Exception as exc:  # no roles: the person can still use the Data access app
        log.warning("Could not read group memberships with the user token: %s", exc)
        return set()


def resolve_app_roles(email: str, token: str | None) -> frozenset[AppRole]:
    settings = get_settings()
    group_map = {
        settings.admin_group.lower(): AppRole.ADMIN,
        settings.security_group.lower(): AppRole.SECURITY,
        settings.auditor_group.lower(): AppRole.AUDITOR,
        settings.break_glass_group.lower(): AppRole.BREAK_GLASS,
    }
    roles = {group_map[g.lower()] for g in groups_from_token(token) if g.lower() in group_map}
    roles |= {AppRole(r) for r in settings.local_dev_roles if r in AppRole.__members__}
    return frozenset(roles)


def current_user() -> CurrentUser:
    """Resolve the caller once per Streamlit session."""
    import streamlit as st

    cached = st.session_state.get("_cdp_security_user")
    if cached is not None:
        return cached
    headers = st.context.headers
    email = headers.get("X-Forwarded-Email") or headers.get("X-Forwarded-Preferred-Username")
    token = headers.get("X-Forwarded-Access-Token")
    settings = get_settings()
    if not email:
        if settings.local_dev_user:
            email = settings.local_dev_user
        else:
            st.error("Your identity was not passed to the app. Open it through the Databricks Apps URL.")
            st.stop()
    email = email.strip().lower()
    user = CurrentUser(
        email=email,
        display_name=headers.get("X-Forwarded-Preferred-Username") or email,
        app_roles=resolve_app_roles(email, token),
    )
    st.session_state["_cdp_security_user"] = user
    return user
