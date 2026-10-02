"""Render the per-app Lakebase grants from sql/lakebase/roles_template.sql."""
from __future__ import annotations

import re


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def render_grants(template: str, request_app_role: str, admin_app_role: str,
                  auditor_role: str | None = None) -> str:
    m = re.search(r"-- BEGIN GRANTS\n(.*?)-- END GRANTS", template, re.S)
    if not m:
        raise ValueError("roles_template.sql has no BEGIN/END GRANTS section")
    if not request_app_role or not admin_app_role or request_app_role == admin_app_role:
        raise ValueError("Two distinct app roles are required")
    lines = []
    for line in m.group(1).splitlines():
        if "{{AUDITOR}}" in line and not auditor_role:
            continue
        lines.append(line.replace("{{REQUEST_APP}}", _ident(request_app_role))
                         .replace("{{ADMIN_APP}}", _ident(admin_app_role))
                         .replace("{{AUDITOR}}", _ident(auditor_role or "")))
    return "\n".join(lines)
