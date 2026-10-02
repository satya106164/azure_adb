"""Lakebase (PostgreSQL) access with OAuth token authentication.

Lakebase accepts a short-lived Databricks OAuth token as the Postgres password.
The token is fetched for the current identity (App service principal or the
job's run-as service principal) and refreshed before it expires; pooled
connections are recycled well inside the token lifetime.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

log = logging.getLogger(__name__)


class _TokenProvider:
    REFRESH_AFTER_SECONDS = 40 * 60  # tokens live ~60 minutes

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._fetched_at = 0.0

    def get(self) -> str:
        with self._lock:
            if self._token is None or time.time() - self._fetched_at > self.REFRESH_AFTER_SECONDS:
                self._token = self._fetch()
                self._fetched_at = time.time()
            return self._token

    @staticmethod
    def _fetch() -> str:
        from databricks.sdk import WorkspaceClient

        settings = get_settings()
        w = WorkspaceClient()
        if settings.lakebase_instance:
            cred = w.database.generate_database_credential(
                request_id=str(uuid.uuid4()), instance_names=[settings.lakebase_instance]
            )
            return cred.token
        # Fallback: the identity's workspace OAuth token
        return w.config.oauth_token().access_token


_tokens = _TokenProvider()


class _OAuthConnection(psycopg.Connection):
    """psycopg connection that injects a fresh OAuth token as the password."""

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs: Any):  # type: ignore[override]
        if not os.environ.get("PGPASSWORD"):  # PGPASSWORD only for local development
            kwargs["password"] = _tokens.get()
        return super().connect(conninfo, **kwargs)


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _resolve_host() -> str:
    settings = get_settings()
    if settings.pg_host:
        return settings.pg_host
    if not settings.lakebase_instance:
        raise RuntimeError("Set PGHOST or LAKEBASE_INSTANCE_NAME.")
    from databricks.sdk import WorkspaceClient

    instance = WorkspaceClient().database.get_database_instance(name=settings.lakebase_instance)
    return instance.read_write_dns


def _resolve_user() -> str:
    settings = get_settings()
    if settings.pg_user:
        return settings.pg_user
    from databricks.sdk import WorkspaceClient

    me = WorkspaceClient().current_user.me()
    return me.user_name  # for a service principal this is its application id


def get_pool() -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            settings = get_settings()
            conninfo = psycopg.conninfo.make_conninfo(
                host=_resolve_host(), port=settings.pg_port, dbname=settings.pg_database,
                user=_resolve_user(), sslmode=settings.pg_sslmode, application_name="cdp_security",
            )
            _pool = ConnectionPool(
                conninfo=conninfo,
                connection_class=_OAuthConnection,
                min_size=1,
                max_size=settings.pool_max,
                max_lifetime=35 * 60,
                kwargs={"row_factory": dict_row, "options": f"-c search_path={settings.db_schema},public"},
                open=True,
            )
            log.info("Lakebase pool opened (max_size=%s)", settings.pool_max)
    return _pool


def reset_pool() -> None:
    """Close the pool (used by tests that switch database roles)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def transaction() -> Iterator[psycopg.Cursor]:
    """One database transaction; commits on success, rolls back on any exception."""
    with get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                yield cur


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with get_pool().connection() as conn:
        yield conn


def fetch_all(sql: str, params: Any = None) -> list[dict]:
    with transaction() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Any = None) -> dict | None:
    with transaction() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
