"""Default entitlement templates and asset privilege rules.

These encode the standard shape of access so admins do not hand-craft grants:
* data product READ  -> USE CATALOG, USE SCHEMA, SELECT, EXECUTE
* data product WRITE -> READ + MODIFY
* AI use case USER   -> run the Genie space / query the endpoints, plus READ on the data product
* AI use case ADMIN  -> manage the Genie space, query endpoints, plus READ on the data product
"""
from __future__ import annotations

from .models import PrivilegeLevel

DATA_PRODUCT_UC: dict[PrivilegeLevel, list[tuple[str, str]]] = {
    PrivilegeLevel.READ: [("CATALOG", "USE CATALOG"), ("SCHEMA", "USE SCHEMA"),
                          ("SCHEMA", "SELECT"), ("SCHEMA", "EXECUTE")],
    PrivilegeLevel.WRITE: [("CATALOG", "USE CATALOG"), ("SCHEMA", "USE SCHEMA"),
                           ("SCHEMA", "SELECT"), ("SCHEMA", "EXECUTE"), ("SCHEMA", "MODIFY")],
}

# Valid privilege values per non-UC asset type (Databricks permission levels / Power BI rights)
ASSET_PRIVILEGES: dict[str, frozenset[str]] = {
    "GENIE_SPACE": frozenset({"CAN_VIEW", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"}),
    "SERVING_ENDPOINT": frozenset({"CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"}),
    "AI_FUNCTION": frozenset({"CAN_QUERY"}),
    "AGENT": frozenset({"CAN_VIEW", "CAN_QUERY", "CAN_MANAGE"}),
    "DASHBOARD": frozenset({"CAN_READ", "CAN_RUN", "CAN_EDIT", "CAN_MANAGE"}),
    "PBI_WORKSPACE": frozenset({"Viewer", "Contributor", "Member", "Admin"}),
    "PBI_SEMANTIC_MODEL": frozenset({"Read", "ReadExplore", "ReadReshare", "ReadReshareExplore"}),
}
# Privileges that confer management rights: only ADMIN-level groups may hold them
ADMIN_ONLY = frozenset({"CAN_MANAGE", "CAN_EDIT", "Admin", "Member", "Contributor", "ReadReshare", "ReadReshareExplore"})

AI_DEFAULTS: dict[tuple[PrivilegeLevel, str], str] = {
    (PrivilegeLevel.USER, "GENIE_SPACE"): "CAN_RUN",
    (PrivilegeLevel.ADMIN, "GENIE_SPACE"): "CAN_MANAGE",
    (PrivilegeLevel.USER, "SERVING_ENDPOINT"): "CAN_QUERY",
    (PrivilegeLevel.ADMIN, "SERVING_ENDPOINT"): "CAN_QUERY",
    (PrivilegeLevel.USER, "AI_FUNCTION"): "CAN_QUERY",
    (PrivilegeLevel.ADMIN, "AI_FUNCTION"): "CAN_QUERY",
    (PrivilegeLevel.USER, "AGENT"): "CAN_QUERY",
    (PrivilegeLevel.ADMIN, "AGENT"): "CAN_MANAGE",
    (PrivilegeLevel.USER, "DASHBOARD"): "CAN_RUN",
    (PrivilegeLevel.ADMIN, "DASHBOARD"): "CAN_MANAGE",
    (PrivilegeLevel.USER, "PBI_WORKSPACE"): "Viewer",
    (PrivilegeLevel.ADMIN, "PBI_WORKSPACE"): "Member",
    (PrivilegeLevel.USER, "PBI_SEMANTIC_MODEL"): "Read",
    (PrivilegeLevel.ADMIN, "PBI_SEMANTIC_MODEL"): "ReadReshare",
}


class TemplateError(ValueError):
    pass


def data_product_entitlements(level: PrivilegeLevel | str) -> list[tuple[str, str]]:
    level = PrivilegeLevel(level)
    if level not in DATA_PRODUCT_UC:
        raise TemplateError(f"No data product template for level {level.value}")
    return list(DATA_PRODUCT_UC[level])


def ai_use_case_entitlements(level: PrivilegeLevel | str, assets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """assets: [(asset_id, asset_type)] -> [(asset_id, privilege)]"""
    level = PrivilegeLevel(level)
    out = []
    for asset_id, asset_type in assets:
        priv = AI_DEFAULTS.get((level, asset_type))
        if priv is None:
            raise TemplateError(f"No AI template for {level.value} on {asset_type}")
        out.append((asset_id, priv))
    return out


def validate_asset_privilege(asset_type: str, privilege: str, group_level: PrivilegeLevel | str) -> str:
    allowed = ASSET_PRIVILEGES.get(asset_type)
    if not allowed or privilege not in allowed:
        raise TemplateError(f"{privilege!r} is not valid for {asset_type} (allowed: {sorted(allowed or [])}).")
    if privilege in ADMIN_ONLY and PrivilegeLevel(group_level) != PrivilegeLevel.ADMIN:
        raise TemplateError(f"{privilege} confers management rights; only ADMIN-level groups may hold it.")
    return privilege
