"""Safe construction of Unity Catalog GRANT / REVOKE statements.

Only privileges on the allow-list can be issued by the app. MANAGE, OWNERSHIP
and ALL PRIVILEGES are never granted through self-service.
"""
from __future__ import annotations

import re

from .naming import GROUP_RE, NamingError

ALLOWED_PRIVILEGES: dict[str, frozenset[str]] = {
    "CATALOG": frozenset({"USE CATALOG", "BROWSE"}),
    "SCHEMA": frozenset({"USE SCHEMA", "SELECT", "MODIFY", "EXECUTE", "READ VOLUME", "WRITE VOLUME",
                         "CREATE TABLE", "CREATE FUNCTION", "CREATE VOLUME", "CREATE MODEL"}),
    "TABLE": frozenset({"SELECT", "MODIFY"}),
    "VOLUME": frozenset({"READ VOLUME", "WRITE VOLUME"}),
    "FUNCTION": frozenset({"EXECUTE"}),
}
NAME_PARTS = {"CATALOG": 1, "SCHEMA": 2, "TABLE": 3, "VOLUME": 3, "FUNCTION": 3}
_IDENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_\-]{0,254}$")


class GrantSqlError(ValueError):
    pass


def quote_ident(part: str) -> str:
    if not _IDENT_RE.match(part):
        raise GrantSqlError(f"Identifier {part!r} contains unsupported characters.")
    return f"`{part}`"


def quote_securable(securable_type: str, full_name: str) -> str:
    parts = full_name.lower().split(".")
    expected = NAME_PARTS.get(securable_type)
    if expected is None:
        raise GrantSqlError(f"Unsupported securable type {securable_type!r}.")
    if len(parts) != expected:
        raise GrantSqlError(f"{securable_type} name must have {expected} part(s): {full_name!r}.")
    return ".".join(quote_ident(p) for p in parts)


def validate_privilege(securable_type: str, privilege: str) -> str:
    priv = " ".join(privilege.upper().split())
    if priv not in ALLOWED_PRIVILEGES.get(securable_type, frozenset()):
        raise GrantSqlError(f"{priv} on {securable_type} is not allowed through this app.")
    return priv


def build_grant_sql(action: str, securable_type: str, full_name: str, privilege: str, group_name: str) -> str:
    action = action.upper()
    if action not in {"GRANT", "REVOKE"}:
        raise GrantSqlError("action must be GRANT or REVOKE")
    securable_type = securable_type.upper()
    priv = validate_privilege(securable_type, privilege)
    target = quote_securable(securable_type, full_name)
    if not GROUP_RE.match(group_name):
        raise NamingError(f"Refusing to grant to non-managed principal {group_name!r}.")
    preposition = "TO" if action == "GRANT" else "FROM"
    return f"{action} {priv} ON {securable_type} {target} {preposition} `{group_name}`"
