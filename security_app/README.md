# CDP Security: Data Access Workflow (Azure Databricks)

Self-service access requests, multi-stage approvals, automated provisioning, recertification, drift
detection and a tamper-evident audit trail for access to Unity Catalog data products, Genie / AI assets
and Power BI. Built for a UK pensions business (FCA, UK GDPR).

- **New to Databricks or to this project? Start with [docs/GUIDE.md](docs/GUIDE.md).** It explains the
  Databricks concepts used, every file in the repository, and exactly how deployment works.
- The functional specification is [docs/requirements.md](docs/requirements.md) (FR-x numbers in the
  code refer to it).

## What gets deployed

| Thing | Dev name | What it is |
|---|---|---|
| Data access app | `cdp-security-dev` | Streamlit app for everyone: request, approve, review, emergency access |
| Catalog admin app | `cdp-security-admin-dev` | Streamlit app for admins, auditors, security |
| Lakebase instance | `cdp-security-dev` | Managed PostgreSQL. App data: database `databricks_postgres`, schema `cdp_security`. Serverless: autoscales and suspends when idle (the apps hold no idle connections) |
| Job | `cdp_security-migrate-dev` | Creates / upgrades the database schema and the reporting tables |
| Job | `cdp_security-provisioning-dev` | Every 15 min (paused in dev): applies approved access, expires grants |
| Job | `cdp_security-daily-dev` | 06:30 daily (paused in dev): lifecycle, recertification, drift, reporting, notify |
| Reporting schema | `dev_gold.cdp_security` | Delta star schema + views. Lives in the **existing** catalog `dev_gold` |

The bundle **never creates a Unity Catalog catalog**. It uses an existing catalog (`catalog` variable).

## Everyday commands

```bash
# The CLI ships with the VS Code Databricks extension; or install it: brew install databricks/tap/databricks
databricks auth login --host https://adb-7405607693517995.15.azuredatabricks.net --profile Dev   # once

cd security_app
databricks bundle validate -t dev -p Dev                    # check the configuration
databricks bundle deploy   -t dev -p Dev                    # create / update everything
databricks bundle run      -t dev -p Dev cdp_security_migrate        # after any SQL change
databricks bundle run      -t dev -p Dev cdp_security_request_app    # (re)start an app with new code
databricks bundle run      -t dev -p Dev cdp_security_admin_app
databricks bundle run      -t dev -p Dev cdp_security_provisioning   # apply approved access now
databricks bundle run      -t dev -p Dev cdp_security_daily
databricks bundle destroy  -t dev -p Dev                    # delete everything the bundle created
```

## Repository layout

| Path | What it is |
|---|---|
| `databricks.yml` | Bundle definition: variables and targets (dev / tst / prd) |
| `resources/` | What gets created: `app.yml` (2 apps), `jobs.yml` (3 jobs), `lakebase.yml` (database instance) |
| `app/request_app.py`, `app/admin_app.py` | Entry points of the two Streamlit apps |
| `app/views/` | One file per page of the apps |
| `app/cdp_security/` | Shared Python package: business rules, database access, provisioning, drift, ... |
| `app/cdp_security/providers/` | Adapters to Entra ID (Graph), Unity Catalog, workspace ACLs, Power BI |
| `jobs/` | Thin entry points that the jobs run |
| `sql/lakebase/` | Postgres schema migrations, reference data, per-app grants |
| `sql/reporting/` | Delta reporting tables and views |
| `sql/uc/` | One-off Unity Catalog grants an admin runs per environment |
| `tests/` | Unit tests + an end-to-end PostgreSQL test |
| `docs/` | Guide and requirements |

## Test

```bash
uv sync --group dev && uv run pytest        # or: pip install pytest -r app/requirements.txt requests && pytest
# End-to-end against an empty local PostgreSQL 15+ (superuser):
CDP_SECURITY_IT_PG=1 PGHOST=localhost PGUSER=postgres PGPASSWORD=... PGSSLMODE=disable pytest tests/test_integration_pg.py
```

## Run an app locally

```bash
cd app
export CDP_SECURITY_ENV=local CDP_SECURITY_LOCAL_USER=you@yourco.com \
       PGHOST=localhost PGUSER=postgres PGPASSWORD=... PGDATABASE=cdp_security PGSSLMODE=disable
streamlit run request_app.py                   # Data access app
streamlit run admin_app.py --server.port 8502  # Catalog admin app
```
