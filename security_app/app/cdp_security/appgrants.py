"""Unity Catalog grants for the two apps' service principals, per table.

Re-applied by the migrate job after every run, so a new table never misses its
grants and removing a grant here removes it in Unity Catalog. Each app gets
SELECT on what it shows and MODIFY only on what it changes, so a write attempted
by the wrong app is refused by Unity Catalog, not just by application code.

  Data access app   requests, approvals, reviews, delegations, owner removals
  Catalog admin app workspaces, data products, groups, manual fulfilment, audit

Both apps append to the audit trail. Appending computes the next hash from the
previous event, so both can also read audit_event (UPDATE / DELETE stay
impossible: the table is append-only).
"""
from __future__ import annotations

import re

VIEWS = ("v_group", "v_request", "v_grant", "v_task")
TABLES = ("classification", "approval_rule", "app_user", "workspace", "data_product", "access_group",
          "delegation", "access_request", "approval_task", "access_grant", "recert_campaign", "recert_item",
          "notification_outbox", "audit_event")

REQUEST_APP_MODIFY = frozenset({"app_user", "delegation", "access_request", "approval_task", "access_grant",
                                "recert_item", "notification_outbox", "audit_event"})
REQUEST_APP_NO_READ: frozenset[str] = frozenset()
ADMIN_APP_MODIFY = frozenset({"app_user", "workspace", "data_product", "access_group", "access_request",
                              "approval_task", "access_grant", "notification_outbox", "audit_event"})

_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,254}$")
_IDENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,254}$")


def _principal(name: str) -> str:
    if not _PRINCIPAL_RE.match(name or ""):
        raise ValueError(f"Not a valid principal: {name!r}")
    return f"`{name}`"


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"Not a valid identifier: {name!r}")
    return f"`{name}`"


def table_privileges(app: str, obj: str) -> list[str]:
    if obj in VIEWS:
        return ["SELECT"]
    if app == "request":
        if obj in REQUEST_APP_NO_READ:
            return []
        return ["SELECT", "MODIFY"] if obj in REQUEST_APP_MODIFY else ["SELECT"]
    return ["SELECT", "MODIFY"] if obj in ADMIN_APP_MODIFY else ["SELECT"]


def render(catalog: str, schema: str, request_app: str, admin_app: str) -> tuple[list[str], list[str]]:
    """(statements that must succeed, catalog-level statements that need catalog ownership)."""
    if not request_app or not admin_app or request_app == admin_app:
        raise ValueError("Two distinct app service principals are required")
    cat, sch = _ident(catalog), f"{_ident(catalog)}.{_ident(schema)}"
    must: list[str] = []
    for app, sp in (("request", _principal(request_app)), ("admin", _principal(admin_app))):
        must.append(f"GRANT USE SCHEMA ON SCHEMA {sch} TO {sp}")
        for obj in TABLES + VIEWS:
            must.append(f"REVOKE ALL PRIVILEGES ON TABLE {sch}.{_ident(obj)} FROM {sp}")
            privileges = table_privileges(app, obj)
            if privileges:
                must.append(f"GRANT {', '.join(privileges)} ON TABLE {sch}.{_ident(obj)} TO {sp}")
    optional = [f"GRANT USE CATALOG ON CATALOG {cat} TO {_principal(p)}" for p in (request_app, admin_app)]
    return must, optional

