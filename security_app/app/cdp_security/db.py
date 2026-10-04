"""Delta tables through a Databricks SQL warehouse.

Every read and write goes through databricks-sql-connector, authenticated as the
current identity: the App's service principal, or the job's run-as identity.
The session's default catalog and schema are the app's, so SQL uses bare table
names. Statements use named parameters (:name); a list value is expanded for
`IN (:name)`.

Delta has no multi-statement transactions. Every rule that must hold under
concurrency is written as ONE statement that checks and changes together
(INSERT ... SELECT ... WHERE NOT EXISTS, UPDATE ... WHERE status = :expected) and
the caller checks the affected row count (`execute` returns it). All tables use
Serializable isolation, so two such statements racing on one table conflict;
the losing statement is retried here, re-evaluating its condition.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from .config import get_settings

log = logging.getLogger(__name__)

_CONFLICT_MARKERS = ("CONCURRENTAPPEND", "CONCURRENTDELETE", "CONCURRENTMODIFICATION", "CONCURRENTTRANSACTION",
                     "DELTA_CONCURRENT", "METADATACHANGED", "PROTOCOLCHANGED")
MAX_CONFLICT_RETRIES = 6
_local = threading.local()


def _connect():
    from databricks import sql
    from databricks.sdk.core import Config

    settings = get_settings()
    if not settings.warehouse_id:
        raise RuntimeError("Set DATABRICKS_WAREHOUSE_ID to the SQL warehouse the app uses.")
    cfg = Config()
    host = cfg.host.removeprefix("https://").removeprefix("http://").rstrip("/")
    conn = sql.connect(
        server_hostname=host,
        http_path=f"/sql/1.0/warehouses/{settings.warehouse_id}",
        credentials_provider=lambda: cfg.authenticate,
        catalog=settings.catalog,
        session_configuration={"TIMEZONE": "UTC"},
        user_agent_entry="cdp_security",
    )
    try:  # the schema is missing only before the first migrate run, which creates it and calls use_schema()
        with conn.cursor() as cur:
            cur.execute(f"USE SCHEMA `{settings.schema}`")
    except Exception as exc:  # noqa: BLE001
        log.warning("Schema %s.%s not usable yet: %s", settings.catalog, settings.schema, str(exc)[:200])
    log.info("Connected to warehouse %s (%s.%s)", settings.warehouse_id, settings.catalog, settings.schema)
    return conn


def use_schema() -> None:
    settings = get_settings()
    execute(f"USE CATALOG `{settings.catalog}`")
    execute(f"USE SCHEMA `{settings.schema}`")


def _connection():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = _connect()
    return conn


def reset() -> None:
    """Drop this thread's connection (the next call reconnects)."""
    conn = getattr(_local, "conn", None)
    _local.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def expand(sql: str, params: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Expand list parameters for IN (...) and normalise values.

    `IN (:ids)` with ids=[a, b] becomes `IN (:ids_0, :ids_1)`; an empty list becomes
    `IN (NULL)`, which matches nothing. Never use a list parameter with NOT IN.
    """
    out: dict[str, Any] = {}
    for name, value in (params or {}).items():
        if isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
            names = [f"{name}_{i}" for i in range(len(values))]
            sql = re.sub(rf":{name}\b", ", ".join(f":{n}" for n in names) or "NULL", sql)
            out.update({n: _value(v) for n, v in zip(names, values)})
        else:
            out[name] = _value(value)
    return sql, out


def _value(v: Any) -> Any:
    return v.value if isinstance(v, Enum) else v


def _row(r: Any) -> dict:
    d = r.asDict() if hasattr(r, "asDict") else dict(r)
    for k, v in d.items():
        if isinstance(v, datetime) and v.tzinfo is None:  # the session runs in UTC
            d[k] = v.replace(tzinfo=timezone.utc)
    return d


def _statement_error() -> type:
    """Errors the warehouse returned for a statement (the connection itself is still fine)."""
    from databricks.sql.exc import ServerOperationError

    return ServerOperationError


def is_conflict(exc: Exception) -> bool:
    text = str(exc).upper().replace("_", "")
    return any(m.replace("_", "") in text for m in _CONFLICT_MARKERS)


def _run(sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
    sql, values = expand(sql, params)
    for attempt in range(1, MAX_CONFLICT_RETRIES + 1):
        try:
            with _connection().cursor() as cur:
                cur.execute(sql, values or None)
                if cur.description is None:
                    return []
                return [_row(r) for r in cur.fetchall()]
        except Exception as exc:
            if is_conflict(exc) and attempt < MAX_CONFLICT_RETRIES:
                wait = min(0.2 * 2 ** attempt, 5) * (0.5 + random.random())
                log.info("Concurrent write conflict (attempt %s), retrying in %.1fs", attempt, wait)
                time.sleep(wait)
                continue
            if not isinstance(exc, _statement_error()):  # connection-level failure: reconnect next time
                reset()
            raise
    raise RuntimeError("unreachable")


def fetch_all(sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
    return _run(sql, params)


def fetch_one(sql: str, params: Mapping[str, Any] | None = None) -> dict | None:
    rows = _run(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Mapping[str, Any] | None = None) -> int:
    """Run a DML / DDL statement and return the number of affected rows (0 for DDL)."""
    rows = _run(sql, params)
    if rows and "num_affected_rows" in rows[0]:
        return int(rows[0]["num_affected_rows"] or 0)
    return 0


def split_statements(text: str) -> list[str]:
    """Split a SQL script into statements. Statements end with ';' at the end of a line,
    so ';' inside string literals (COMMENT 'a; b') is safe. Line comments are removed."""
    text = re.sub(r"--[^\n]*", "", text)
    return [s.strip() for s in re.split(r";[ \t]*(?:\n|$)", text) if s.strip()]


def run_script(text: str, substitutions: Mapping[str, str] | None = None) -> int:
    for key, value in (substitutions or {}).items():
        text = text.replace("${" + key + "}", value)
    statements = split_statements(text)
    for stmt in statements:
        log.info("SQL: %s", stmt.splitlines()[0][:120])
        try:
            execute(stmt)
        except Exception as exc:
            # Re-running a half-applied migration: a constraint or column added last time is not an error
            upper = stmt.upper()
            if ("ADD CONSTRAINT" in upper or "ADD COLUMNS" in upper) and "ALREADY_EXISTS" in str(exc).upper():
                log.info("Already present; skipped")
                continue
            raise
    return len(statements)


def now() -> datetime:
    return datetime.now(timezone.utc)
