"""Create / upgrade the Delta tables, views and reporting model, then the apps' grants.

Numbered migrations (sql/delta/NNN_*.sql) run once and are tracked with a
checksum; editing an applied file fails the job (write a new migration instead).
views.sql, sql/reporting/reporting.sql and the per-app grants are re-applied on
every run."""
import glob
import hashlib
import logging
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap

args = bootstrap([("--request-app-sp", {"default": ""}), ("--admin-app-sp", {"default": ""}),
                  ("--reader-groups", {"default": ""})])
from cdp_security import appgrants, db  # noqa: E402

log = logging.getLogger("migrate")
root = args.bundle_root

db.execute(f"CREATE SCHEMA IF NOT EXISTS `{args.catalog}`.`{args.schema}` "
           "COMMENT 'Security access workflow - operational tables, audit trail and reporting model'")
db.use_schema()
db.execute("CREATE TABLE IF NOT EXISTS schema_migration (version STRING NOT NULL, checksum STRING NOT NULL, "
           "applied_at TIMESTAMP NOT NULL)")
applied = {r["version"]: r["checksum"] for r in db.fetch_all("SELECT version, checksum FROM schema_migration")}

for path in sorted(glob.glob(os.path.join(root, "sql", "delta", "[0-9][0-9][0-9]_*.sql"))):
    version = os.path.basename(path)
    text = open(path, encoding="utf-8").read()
    checksum = hashlib.sha256(text.encode()).hexdigest()
    if version in applied:
        if applied[version] != checksum:
            raise RuntimeError(f"{version} changed after it was applied; add a new migration instead")
        continue
    # No multi-statement transactions: a migration that fails half-way is re-run from the top,
    # so write them idempotently (IF NOT EXISTS / IF EXISTS).
    log.info("Applying %s", version)
    db.run_script(text)
    db.execute("INSERT INTO schema_migration VALUES (:v, :c, current_timestamp())", {"v": version, "c": checksum})

for path in (os.path.join(root, "sql", "delta", "views.sql"), os.path.join(root, "sql", "reporting", "reporting.sql")):
    log.info("Applying %s", os.path.relpath(path, root))
    db.run_script(open(path, encoding="utf-8").read())

if args.request_app_sp and args.admin_app_sp:
    must, optional = appgrants.render(args.catalog, args.schema, args.request_app_sp, args.admin_app_sp)
    for stmt in must:
        db.execute(stmt)
    for stmt in optional:  # needs ownership of the catalog; otherwise a catalog admin runs sql/uc/001_platform_grants.sql
        try:
            db.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not run %s (%s). Ask a catalog admin to grant it.", stmt, str(exc)[:200])
    log.info("Applied grants for request app %s and admin app %s", args.request_app_sp, args.admin_app_sp)
else:
    log.warning("App service principals not passed: app grants NOT applied.")

for stmt in appgrants.render_readers(args.catalog, args.schema, [g for g in args.reader_groups.split(",") if g]):
    try:
        db.execute(stmt)
    except Exception as exc:  # noqa: BLE001 - e.g. the group does not exist yet in this workspace
        log.warning("Could not run %s (%s)", stmt, str(exc)[:200])
log.info("Migrations complete")
