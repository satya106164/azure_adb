# CDP Security: Data Access Workflow (Azure Databricks)

Self-service access requests, multi-stage approvals, recertification and a tamper-evident audit trail for
access to data products (AI/BI dashboards, Power BI, Genie and custom agents, Databricks apps, Unity Catalog
schemas). The app records and approves; admins change group membership by hand and record each change, so
every add and removal is attributed for audit and reporting. Built for a UK
pensions business (FCA, UK GDPR). All data lives in Delta tables, read and written through a SQL warehouse.

- **New to Databricks or to this project? Start with [docs/GUIDE.md](docs/GUIDE.md).** It explains the
  Databricks concepts used, every file in the repository, and exactly how deployment works.
- The functional specification is [docs/requirements.md](docs/requirements.md) (FR-x numbers in the
  code refer to it).

## What gets deployed

| Thing | Dev name | What it is |
|---|---|---|
| Data access app | `cdp-security-dev` | Streamlit app for everyone: request, approve, review, emergency access |
| Catalog admin app | `cdp-security-admin-dev` | Streamlit app for admins, auditors, security: workspaces, data products, groups, fulfilment, audit |
| Job | `cdp_security-migrate-dev` | Creates / upgrades the Delta tables, views, reporting model and the apps' table grants |
| Job | `cdp_security-notify-dev` | Every 15 min (paused in dev): sends queued notifications |
| Job | `cdp_security-daily-dev` | 06:30 daily (paused in dev): lifecycle, recertification, reporting, notify |
| Schema | `dev_gold.cdp_security` | All Delta tables (operational, audit, reporting). Lives in the **existing** catalog `dev_gold` |

Not deployed by the bundle: the SQL warehouse (dev: *Serverless Starter Warehouse* `3563e74db5969657`).

The bundle **never creates a Unity Catalog catalog**. It uses an existing catalog (`catalog` variable).

## Everyday commands

```bash
# The CLI ships with the VS Code Databricks extension; or install it: brew install databricks/tap/databricks
databricks auth login --host https://adb-7405607693517995.15.azuredatabricks.net --profile Dev   # once

cd security_app
databricks bundle validate -t dev -p Dev                    # check the configuration
databricks bundle deploy   -t dev -p Dev                    # create / update everything
databricks bundle run      -t dev -p Dev cdp_security_migrate        # first deploy, and after any SQL change
databricks bundle run      -t dev -p Dev cdp_security_request_app    # (re)start an app with new code
databricks bundle run      -t dev -p Dev cdp_security_admin_app
databricks bundle run      -t dev -p Dev cdp_security_notify         # send queued notifications now
databricks bundle run      -t dev -p Dev cdp_security_daily
databricks bundle destroy  -t dev -p Dev                    # delete everything the bundle created
```

## Repository layout

| Path | What it is |
|---|---|
| `databricks.yml` | Bundle definition: variables and targets (dev / tst / prd) |
| `resources/` | What gets created: `app.yml` (2 apps), `jobs.yml` (3 jobs) |
| `app/request_app.py`, `app/admin_app.py` | Entry points of the two Streamlit apps |
| `app/views/` | One file per page of the apps |
| `app/cdp_security/` | Shared Python package: business rules, database access, lifecycle, notifications, ... |
| `app/cdp_security/providers/` | Adapter to Microsoft Graph (people lookups and email only) |
| `jobs/` | Thin entry points that the jobs run |
| `sql/delta/` | Delta table migrations, reference data, views |
| `sql/reporting/` | Reporting star schema and business views |
| `sql/uc/` | One-off Unity Catalog grants an admin runs per environment |
| `tests/` | Unit tests |
| `docs/` | Guide and requirements |

## Test

```bash
uv sync --group dev && uv run pytest        # or: pip install pytest -r app/requirements.txt requests && pytest
```

## Run an app locally

Uses the `Dev` profile and the dev warehouse and tables. Roles come from `CDP_SECURITY_LOCAL_ROLES`.

```bash
cd app
export CDP_SECURITY_ENV=local CDP_SECURITY_LOCAL_USER=you@yourco.com CDP_SECURITY_LOCAL_ROLES=ADMIN \
       DATABRICKS_CONFIG_PROFILE=Dev DATABRICKS_WAREHOUSE_ID=3563e74db5969657 \
       CDP_SECURITY_CATALOG=dev_gold CDP_SECURITY_SCHEMA=cdp_security
streamlit run request_app.py                   # Data access app
streamlit run admin_app.py --server.port 8502  # Catalog admin app
```

With python.org Python on macOS, also `export SSL_CERT_FILE=$(python -m certifi)`: that Python build has no
root certificates of its own, and the SQL connector fails with `CERTIFICATE_VERIFY_FAILED` without them.
