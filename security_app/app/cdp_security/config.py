"""Runtime configuration, read once from environment variables.

The App receives these through the bundle's app `config.env` block; jobs receive
them as task parameters which jobs/_common.py copies into the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

ENV_CODES = {"local": "dev", "dev": "dev", "test": "tst", "tst": "tst", "prod": "prd", "prd": "prd"}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _bool(name: str, default: bool) -> bool:
    return str(_env(name, str(default))).strip().lower() in {"1", "true", "yes", "y"}


def _list(name: str) -> tuple[str, ...]:
    raw = _env(name, "") or ""
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    env: str                      # dev | tst | prd (normalised)
    raw_env: str                  # as configured, e.g. "local"
    db_schema: str
    lakebase_instance: str | None
    pg_host: str | None
    pg_port: int
    pg_database: str
    pg_user: str | None
    pg_sslmode: str
    pool_max: int
    admin_group: str
    security_group: str
    auditor_group: str
    break_glass_group: str
    security_mailbox: str | None
    admin_mailbox: str | None
    app_url: str | None           # Data access (request) app URL, used in notifications
    admin_app_url: str | None     # Catalog admin app URL
    dry_run: bool
    graph_create_groups: bool
    break_glass_hours: int
    break_glass_review_hours: int
    auto_reject_after_hours: int
    recert_window_days: int
    holidays: frozenset[date]
    drift_allowlist: tuple[str, ...]
    local_dev_user: str | None
    timezone: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    raw_env = (_env("CDP_SECURITY_ENV", "dev") or "dev").lower()
    env = ENV_CODES.get(raw_env)
    if env is None:
        raise RuntimeError(f"CDP_SECURITY_ENV must be one of {sorted(ENV_CODES)}, got {raw_env!r}")
    holidays = frozenset(date.fromisoformat(d) for d in _list("CDP_SECURITY_HOLIDAYS"))
    return Settings(
        env=env,
        raw_env=raw_env,
        db_schema=_env("CDP_SECURITY_DB_SCHEMA", "cdp_security"),
        lakebase_instance=_env("LAKEBASE_INSTANCE_NAME"),
        pg_host=_env("PGHOST"),
        pg_port=int(_env("PGPORT", "5432")),
        pg_database=_env("PGDATABASE", "databricks_postgres"),
        pg_user=_env("PGUSER"),
        pg_sslmode=_env("PGSSLMODE", "require"),
        pool_max=int(_env("CDP_SECURITY_POOL_MAX", "8")),
        admin_group=_env("CDP_SECURITY_ADMIN_GROUP", f"grp-dbx-{env}-app-cdp_security-admin"),
        security_group=_env("CDP_SECURITY_SECURITY_GROUP", f"grp-dbx-{env}-app-cdp_security-security"),
        auditor_group=_env("CDP_SECURITY_AUDITOR_GROUP", f"grp-dbx-{env}-app-cdp_security-auditor"),
        break_glass_group=_env("CDP_SECURITY_BREAK_GLASS_GROUP", f"grp-dbx-{env}-app-cdp_security-breakglass"),
        security_mailbox=_env("CDP_SECURITY_SECURITY_MAILBOX"),
        admin_mailbox=_env("CDP_SECURITY_ADMIN_MAILBOX"),
        app_url=_env("CDP_SECURITY_APP_URL"),
        admin_app_url=_env("CDP_SECURITY_ADMIN_APP_URL"),
        # Safe default: nothing is changed in Entra / UC / Power BI unless explicitly enabled.
        dry_run=_bool("CDP_SECURITY_DRY_RUN", True),
        graph_create_groups=_bool("CDP_SECURITY_GRAPH_CREATE_GROUPS", False),
        break_glass_hours=int(_env("CDP_SECURITY_BREAK_GLASS_HOURS", "4")),
        break_glass_review_hours=int(_env("CDP_SECURITY_BREAK_GLASS_REVIEW_HOURS", "24")),
        auto_reject_after_hours=int(_env("CDP_SECURITY_AUTO_REJECT_AFTER_HOURS", "240")),
        recert_window_days=int(_env("CDP_SECURITY_RECERT_WINDOW_DAYS", "14")),
        holidays=holidays,
        drift_allowlist=tuple(x.lower() for x in _list("CDP_SECURITY_DRIFT_ALLOWLIST")),
        local_dev_user=_env("CDP_SECURITY_LOCAL_USER") if raw_env == "local" else None,
        timezone=_env("CDP_SECURITY_TIMEZONE", "Europe/London"),
    )
