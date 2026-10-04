"""Pure parts of the Delta / warehouse layer: parameter handling, scripts, grants, generated SQL."""
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cdp_security import appgrants, audit, db, service
from cdp_security.models import RequestStatus

ROOT = Path(__file__).resolve().parent.parent


# ---- parameters ------------------------------------------------------------------
def test_list_parameters_expand_for_in():
    sql, params = db.expand("SELECT * FROM t WHERE s IN (:s) AND id = :id", {"s": ["A", "B"], "id": 7})
    assert sql == "SELECT * FROM t WHERE s IN (:s_0, :s_1) AND id = :id"
    assert params == {"s_0": "A", "s_1": "B", "id": 7}


def test_empty_list_matches_nothing():
    sql, params = db.expand("SELECT 1 FROM t WHERE id IN (:ids)", {"ids": []})
    assert sql == "SELECT 1 FROM t WHERE id IN (NULL)" and params == {}


def test_expansion_does_not_touch_longer_names():
    sql, _ = db.expand("WHERE a IN (:ids) AND b = :ids_total", {"ids": ["x"], "ids_total": 1})
    assert sql == "WHERE a IN (:ids_0) AND b = :ids_total"


def test_enums_become_plain_strings():
    _, params = db.expand("x = :s", {"s": RequestStatus.APPROVED})
    assert params["s"] == "APPROVED" and type(params["s"]) is str


def test_conflicts_are_recognised():
    assert db.is_conflict(Exception("[DELTA_CONCURRENT_APPEND] ConcurrentAppendException: files were added"))
    assert not db.is_conflict(Exception("[TABLE_OR_VIEW_NOT_FOUND] x"))


# ---- scripts ---------------------------------------------------------------------------
def test_split_keeps_semicolons_inside_literals():
    stmts = db.split_statements("-- c\nCREATE TABLE a (x INT) COMMENT 'one; two';\nINSERT INTO a VALUES (1);\n")
    assert stmts == ["CREATE TABLE a (x INT) COMMENT 'one; two'", "INSERT INTO a VALUES (1)"]


@pytest.mark.parametrize("path", sorted((ROOT / "sql").rglob("*.sql")), ids=lambda p: p.name)
def test_sql_scripts_split_cleanly(path):
    for stmt in db.split_statements(path.read_text()):
        assert stmt.count("(") == stmt.count(")"), stmt[:80]
        assert stmt.count("'") % 2 == 0, stmt[:80]


def test_every_table_the_apps_use_is_created():
    migrations = "".join(p.read_text() for p in sorted((ROOT / "sql/delta").glob("[0-9][0-9][0-9]_*.sql")))
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", migrations))
    dropped = set(re.findall(r"DROP TABLE IF EXISTS (\w+)", migrations))
    views = set(re.findall(r"CREATE OR REPLACE VIEW (\w+)", (ROOT / "sql/delta/views.sql").read_text()))
    assert set(appgrants.TABLES) == created - dropped
    assert set(appgrants.VIEWS) == views


# ---- per-app grants ----------------------------------------------------------------------
def test_apps_get_separate_write_rights():
    must, optional = appgrants.render("dev_gold", "cdp_security", "req-sp", "adm-sp")
    grants = [s for s in must if s.startswith("GRANT")]
    assert "GRANT SELECT, MODIFY ON TABLE `dev_gold`.`cdp_security`.`access_request` TO `req-sp`" in grants
    assert "GRANT SELECT ON TABLE `dev_gold`.`cdp_security`.`workspace` TO `req-sp`" in grants
    assert "GRANT SELECT, MODIFY ON TABLE `dev_gold`.`cdp_security`.`workspace` TO `adm-sp`" in grants
    assert "GRANT SELECT ON TABLE `dev_gold`.`cdp_security`.`access_group` TO `req-sp`" in grants
    assert not any("MODIFY" in s and "`v_" in s for s in grants)
    assert optional == ["GRANT USE CATALOG ON CATALOG `dev_gold` TO `req-sp`",
                        "GRANT USE CATALOG ON CATALOG `dev_gold` TO `adm-sp`"]


def test_grants_refuse_odd_principals():
    with pytest.raises(ValueError):
        appgrants.render("dev_gold", "cdp_security", "x`; DROP", "adm")
    with pytest.raises(ValueError):
        appgrants.render("dev_gold", "cdp_security", "same", "same")


# ---- generated SQL ------------------------------------------------------------------------
def test_audit_statements_are_fully_formatted():
    for sql in (audit._INSERT, audit._VERIFY):
        assert "{" not in sql and "}" not in sql
    # writing and verifying use the one hash expression
    assert audit._INSERT.count("sha2(concat_ws('|'") == audit._VERIFY.count("sha2(concat_ws('|'") == 1


def test_generated_codes_never_truncate():
    assert service._next_code("WS", 4) == ("concat('WS-', lpad(CAST(s.seq AS STRING), "
                                           "greatest(4, length(CAST(s.seq AS STRING))), '0'))")


def test_workspace_id_comes_from_the_url():
    assert service.databricks_workspace_id("https://adb-7405607693517995.15.azuredatabricks.net") == 7405607693517995
    assert service.databricks_workspace_id("https://example.com") is None


def test_rows_get_utc_timestamps():
    class R(dict):
        def asDict(self):
            return dict(self)
    row = db._row(R(at=datetime(2026, 10, 4, 9, 0)))
    assert row["at"].tzinfo == timezone.utc
