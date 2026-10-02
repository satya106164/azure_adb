"""Apply Lakebase migrations (sql/lakebase/NNN_*.sql) and the Delta reporting DDL.

Idempotent: applied Lakebase files are tracked with a checksum; editing an
applied file fails the job (write a new migration instead)."""
import glob
import hashlib
import logging
import os
import re
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap, spark_session

args = bootstrap()
from cdp_security.db import connection  # noqa: E402

log = logging.getLogger("migrate")

with connection() as conn:
    conn.autocommit = True
    conn.execute("CREATE SCHEMA IF NOT EXISTS cdp_security")
    conn.execute("CREATE TABLE IF NOT EXISTS cdp_security.schema_migration (version text PRIMARY KEY, "
                 "checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
    applied = {r["version"]: r["checksum"] for r in conn.execute("SELECT * FROM cdp_security.schema_migration").fetchall()}
    conn.autocommit = False
    for path in sorted(glob.glob(os.path.join(args.bundle_root, "sql", "lakebase", "[0-9][0-9][0-9]_*.sql"))):
        version = os.path.basename(path)
        text = open(path, encoding="utf-8").read()
        checksum = hashlib.sha256(text.encode()).hexdigest()
        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(f"{version} changed after it was applied; add a new migration instead")
            continue
        log.info("Applying %s", version)
        with conn.transaction():
            conn.execute(text)
            conn.execute("INSERT INTO cdp_security.schema_migration (version, checksum) VALUES (%s, %s)",
                         (version, checksum))

    # Per-app grants (re-applied every run so new tables are covered)
    if args.request_app_role and args.admin_app_role:
        from cdp_security.dbroles import render_grants

        template = open(os.path.join(args.bundle_root, "sql", "lakebase", "roles_template.sql"),
                        encoding="utf-8").read()
        with conn.transaction():
            conn.execute(render_grants(template, args.request_app_role, args.admin_app_role,
                                       args.auditor_role or None))
        log.info("Applied grants for request app %s and admin app %s", args.request_app_role, args.admin_app_role)
    else:
        log.warning("request_app_role / admin_app_role not set: app grants NOT applied. "
                    "Set them after the first deploy and re-run this job.")

if args.reporting_catalog:
    spark = spark_session()
    for path in sorted(glob.glob(os.path.join(args.bundle_root, "sql", "reporting", "*.sql"))):
        text = open(path, encoding="utf-8").read()
        text = text.replace("${catalog}", args.reporting_catalog).replace("${schema}", args.reporting_schema)
        text = re.sub(r"--[^\n]*", "", text)
        # Statements end with ";" at end of line; a bare split(";") would cut string literals
        # such as COMMENT 'Calendar; UK fiscal year ...'.
        for stmt in [s.strip() for s in re.split(r";[ \t]*(?:\n|$)", text) if s.strip()]:
            log.info("Reporting DDL: %s", stmt.splitlines()[0][:120])
            spark.sql(stmt)
log.info("Migrations complete")
