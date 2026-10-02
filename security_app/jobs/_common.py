"""Shared job bootstrap: parse parameters, expose the shared package, build providers.

All jobs run as the provisioning service principal (bundle run_as). Secrets come
from a Databricks secret scope backed by Azure Key Vault.
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
    p.add_argument("--lakebase-instance", required=True)
    p.add_argument("--pg-database", default="databricks_postgres")
    p.add_argument("--secret-scope", default="cdp_security")
    p.add_argument("--dry-run", default="true")
    p.add_argument("--graph-create-groups", default="false")
    p.add_argument("--reporting-catalog", default="")
    p.add_argument("--reporting-schema", default="reporting")
    p.add_argument("--security-mailbox", default="")
    p.add_argument("--admin-mailbox", default="")
    p.add_argument("--app-url", default="")
    p.add_argument("--drift-allowlist", default="")
    p.add_argument("--request-app-role", default="")
    p.add_argument("--admin-app-role", default="")
    p.add_argument("--auditor-role", default="")
    for name, kwargs in extra or []:
        p.add_argument(name, **kwargs)
    args, _ = p.parse_known_args()

    sys.path.insert(0, os.path.join(args.bundle_root, "app"))
    os.environ.update({
        "CDP_SECURITY_ENV": args.env,
        "LAKEBASE_INSTANCE_NAME": args.lakebase_instance,
        "PGDATABASE": args.pg_database,
        "CDP_SECURITY_DRY_RUN": args.dry_run,
        "CDP_SECURITY_GRAPH_CREATE_GROUPS": args.graph_create_groups,
        "CDP_SECURITY_SECURITY_MAILBOX": args.security_mailbox,
        "CDP_SECURITY_ADMIN_MAILBOX": args.admin_mailbox,
        "CDP_SECURITY_APP_URL": args.app_url,
        "CDP_SECURITY_DRIFT_ALLOWLIST": args.drift_allowlist,
        "CDP_SECURITY_POOL_MAX": "4",
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


def spark_session():
    from pyspark.sql import SparkSession

    return SparkSession.builder.getOrCreate()


def provisioning_principal() -> str:
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient().current_user.me().user_name
