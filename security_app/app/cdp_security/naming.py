"""Access group names.

Groups are created in Entra ID / Databricks by the identity team; the Catalog
admin app only records them, so a name is checked for shape, not existence.
The suggested convention is

    grp-dbx-<env>-dp-<name>-read          e.g. grp-dbx-prd-dp-member_contributions-read
    grp-dbx-<env>-app-cdp_security-<role> this app's own role groups (admin, security, auditor, breakglass)
"""
from __future__ import annotations

import re

GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{2,255}$")


class NamingError(ValueError):
    pass


def validate_group_name(name: str) -> str:
    """Return the name, trimmed, or raise NamingError."""
    name = (name or "").strip()
    if not GROUP_NAME_RE.match(name) or "  " in name:
        raise NamingError("A group name is 3 to 256 characters: letters, digits, spaces and . _ - ( ), "
                          "starting with a letter or digit.")
    return name


def suggest_group_name(env: str, data_product_name: str) -> str:
    """Default name the admin can accept or change: grp-dbx-<env>-dp-<name>-read."""
    name = re.sub(r"[^a-z0-9]+", "_", (data_product_name or "").lower()).strip("_")[:41]
    return f"grp-dbx-{env}-dp-{name}-read"
