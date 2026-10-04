"""Small Streamlit helpers shared by pages."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Iterable

import streamlit as st

from .identity import current_user
from .models import AppRole, CurrentUser
from .service import ServiceError

log = logging.getLogger("cdp_security.ui")

CLASS_COLOURS = {"GENERAL": "green", "CONFIDENTIAL": "orange", "RESTRICTED": "red"}
STATUS_COLOURS = {"ACTIVE": "green", "FULFILLED": "green", "APPROVED": "blue", "PENDING_APPROVAL": "orange",
                  "PENDING_PROVISION": "blue", "PENDING_REVOKE": "orange", "REJECTED": "red", "CANCELLED": "gray",
                  "EXPIRED": "gray", "REVOKED": "gray"}
STATUS_LABELS = {"APPROVED": "approved - awaiting admin", "PENDING_PROVISION": "awaiting admin to add",
                 "PENDING_REVOKE": "awaiting admin to remove"}


def user() -> CurrentUser:
    return current_user()


def require(*roles: AppRole) -> CurrentUser:
    u = current_user()
    if not any(u.has(r) for r in roles):
        st.error("You do not have access to this page.")
        st.stop()
    return u


def classification(code: str) -> str:
    return f":{CLASS_COLOURS.get(code, 'gray')}[**{code.title()}**]"


def status(code: str) -> str:
    text = STATUS_LABELS.get(code, code.replace("_", " ").lower())
    return f":{STATUS_COLOURS.get(code, 'gray')}[{text}]"


def flash(message: str) -> None:
    st.session_state["_flash"] = message


def flash_result(row: dict, key: str, template: str) -> dict:
    """Flash a message naming a generated identifier, e.g. "Workspace WS-0003 registered."."""
    flash(template.format(row[key]))
    return row


def show_flash() -> None:
    msg = st.session_state.pop("_flash", None)
    if msg:
        st.success(msg)


def act(fn: Callable[[], Any], success: str | None = None, rerun: bool = True) -> Any:
    """Run a service call; show validation errors plainly and log unexpected ones with a reference."""
    try:
        result = fn()
    except ServiceError as exc:
        st.error(str(exc))
        return None
    except Exception:  # noqa: BLE001
        ref = uuid.uuid4().hex[:8]
        log.exception("Unexpected error ref=%s", ref)
        st.error(f"Something failed on our side. Quote reference {ref} to the platform team.")
        return None
    if success:
        flash(success)
    if rerun:
        st.rerun()
    return result


def table(rows: list[dict], columns: Iterable[str] | None = None, **kwargs) -> None:
    if not rows:
        st.caption("Nothing to show.")
        return
    cols = list(columns) if columns else list(rows[0].keys())
    st.dataframe([{c: r.get(c) for c in cols} for r in rows], hide_index=True, use_container_width=True, **kwargs)


def label(row: dict, *keys: str, sep: str = " - ") -> str:
    return sep.join(str(row[k]) for k in keys if row.get(k))


def sidebar(user: CurrentUser, app_title: str, other_app: tuple[str, str | None] | None = None) -> None:
    """Common sidebar: environment banner, identity, link to the other app."""
    from .config import get_settings

    settings = get_settings()
    with st.sidebar:
        st.markdown(f"### {app_title}")
        if settings.env != "prd":
            st.warning(f"{settings.env.upper()} environment"
                       + (" - notifications are logged, not sent" if settings.dry_run else ""))
        st.markdown(f"**{user.display_name}**")
        st.caption(user.email)
        if user.app_roles:
            st.caption("Roles: " + ", ".join(sorted(r.value.replace("_", " ").title() for r in user.app_roles)))
        if other_app and other_app[1]:
            st.link_button(other_app[0], other_app[1], use_container_width=True)
        if st.button("Refresh my roles", use_container_width=True):
            st.session_state.pop("_cdp_security_user", None)
            st.rerun()
