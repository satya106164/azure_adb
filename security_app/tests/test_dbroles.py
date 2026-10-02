import os

import pytest

from cdp_security.dbroles import render_grants

TEMPLATE = open(os.path.join(os.path.dirname(__file__), "..", "sql", "lakebase", "roles_template.sql")).read()


def test_render_substitutes_and_quotes():
    sql = render_grants(TEMPLATE, "aaaa-1111", "bbbb-2222", "grp auditors")
    assert '"aaaa-1111"' in sql and '"bbbb-2222"' in sql and '"grp auditors"' in sql
    assert "{{" not in sql and "databricks_create_role" not in sql


def test_render_skips_auditor_when_absent():
    sql = render_grants(TEMPLATE, "a", "b")
    assert "{{AUDITOR}}" not in sql and '""' not in sql


def test_render_needs_two_distinct_roles():
    with pytest.raises(ValueError):
        render_grants(TEMPLATE, "same", "same")


def test_request_app_never_gets_reference_writes():
    sql = render_grants(TEMPLATE, "REQ", "ADM")
    for line in sql.splitlines():
        if line.startswith("GRANT") and '"REQ"' in line and "INSERT" in line:
            assert "data_product" not in line.split("ON")[1].split("TO")[0].replace("project_data_product", "")
