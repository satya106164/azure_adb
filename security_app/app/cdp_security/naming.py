"""Entra ID group naming convention.

    grp-dbx-<env>-<purpose>-<code>-<level>

    env      dev | tst | prd
    purpose  dp (data product) | prj (project) | ai (AI/Genie use case) | ws (workspace) | app (this app)
    code     lower snake case, 2-41 chars (the data product / project / use case code)
    level    read | write | contrib | user | admin

Examples
    grp-dbx-prd-dp-member_contributions-read
    grp-dbx-prd-ai-member_contributions_genie-user   (two groups per AI use case: -user and -admin)
    grp-dbx-prd-prj-pension_dashboard_2026-contrib
"""
from __future__ import annotations

import re

from .models import GroupPurpose, PrivilegeLevel

PURPOSE_CODES = {
    GroupPurpose.DATA_PRODUCT: "dp",
    GroupPurpose.PROJECT: "prj",
    GroupPurpose.AI_USE_CASE: "ai",
    GroupPurpose.WORKSPACE: "ws",
}

ALLOWED_LEVELS = {
    GroupPurpose.DATA_PRODUCT: {PrivilegeLevel.READ, PrivilegeLevel.WRITE},
    GroupPurpose.PROJECT: {PrivilegeLevel.READ, PrivilegeLevel.CONTRIB, PrivilegeLevel.ADMIN},
    GroupPurpose.AI_USE_CASE: {PrivilegeLevel.USER, PrivilegeLevel.ADMIN},
    GroupPurpose.WORKSPACE: {PrivilegeLevel.USER, PrivilegeLevel.ADMIN},
}

CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,40}$")
GROUP_RE = re.compile(
    r"^grp-dbx-(?P<env>dev|tst|prd)-(?P<purpose>dp|prj|ai|ws|app)-"
    r"(?P<code>[a-z0-9][a-z0-9_]{1,40})-(?P<level>read|write|contrib|user|admin)$"
)


class NamingError(ValueError):
    pass


def validate_code(code: str) -> str:
    if not CODE_RE.match(code or ""):
        raise NamingError(f"Code {code!r} must be lower case letters, digits and underscores (2-41 chars).")
    return code


def group_name(env: str, purpose: GroupPurpose | str, code: str, level: PrivilegeLevel | str) -> str:
    purpose = GroupPurpose(purpose)
    level = PrivilegeLevel(level)
    if level not in ALLOWED_LEVELS[purpose]:
        allowed = ", ".join(sorted(x.value for x in ALLOWED_LEVELS[purpose]))
        raise NamingError(f"Level {level.value} is not valid for {purpose.value} groups (allowed: {allowed}).")
    name = f"grp-dbx-{env}-{PURPOSE_CODES[purpose]}-{validate_code(code)}-{level.value.lower()}"
    validate_group_name(name)
    return name


def validate_group_name(name: str, env: str | None = None) -> dict[str, str]:
    match = GROUP_RE.match(name or "")
    if not match:
        raise NamingError(f"Group name {name!r} does not follow grp-dbx-<env>-<purpose>-<code>-<level>.")
    if env and match["env"] != env:
        raise NamingError(f"Group {name!r} belongs to environment {match['env']}, not {env}.")
    return match.groupdict()


def is_managed_group(name: str, env: str) -> bool:
    match = GROUP_RE.match((name or "").lower())
    return bool(match and match["env"] == env and match["purpose"] != "app")


def mail_nickname(name: str) -> str:
    """Entra mailNickname: <= 64 chars, no spaces. Required even for security groups."""
    return re.sub(r"[^A-Za-z0-9._-]", "", name)[:64]
