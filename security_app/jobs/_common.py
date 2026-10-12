"""Shared job bootstrap: parse parameters, expose the shared package, build providers.

Jobs run as the jobs service principal (bundle run_as; in dev, the
deploying user). They never change groups or grants. Every read and write goes through the SQL warehouse, like the
apps. Secrets come from a Databricks secret scope backed by Azure Key Vault.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys


def bootstrap(extra: list[tuple[str, dict]] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--bundle-root", required=True)
    p.add_argument("--env", required=True)
    p.add_argument("--warehouse-id", required=True)
    p.add_argument("--catalog", required=True)
    p.add_argument("--schema", required=True)
    p.add_argument("--secret-scope", default="cdp_security")
    p.add_argument("--dry-run", default="true")
    p.add_argument("--admin-mailbox", default="")
    p.add_argument("--app-url", default="")
    p.add_argument("--admin-app-url", default="")
    p.add_argument("--recert-interval-days", default="365")
    for name, kwargs in extra or []:
        p.add_argument(name, **kwargs)
    args, _ = p.parse_known_args()

    sys.path.insert(0, os.path.join(args.bundle_root, "app"))
    os.environ.update({
        "CDP_SECURITY_ENV": args.env,
        "DATABRICKS_WAREHOUSE_ID": args.warehouse_id,
        "CDP_SECURITY_CATALOG": args.catalog,
        "CDP_SECURITY_SCHEMA": args.schema,
        "CDP_SECURITY_DRY_RUN": args.dry_run,
        "CDP_SECURITY_ADMIN_MAILBOX": args.admin_mailbox,
        "CDP_SECURITY_APP_URL": args.app_url,
        "CDP_SECURITY_ADMIN_APP_URL": args.admin_app_url,
        "CDP_SECURITY_RECERT_INTERVAL_DAYS": args.recert_interval_days,
    })
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return args


def secret(scope: str, key: str, required: bool = True) -> str | None:
    from databricks.sdk.runtime import dbutils

    try:
        return dbutils.secrets.get(scope, key)
    except Exception:
        if required:
            raise
        return None


def graph_provider(args):
    from cdp_security.providers.entra_graph import EntraGraphProvider

    if args.dry_run.lower() == "true" and not secret(args.secret_scope, "graph-client-id", required=False):
        return None
    return EntraGraphProvider(secret(args.secret_scope, "azure-tenant-id"),
                              secret(args.secret_scope, "graph-client-id"),
                              secret(args.secret_scope, "graph-client-secret"))
