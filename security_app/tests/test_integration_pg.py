"""End-to-end test against a real PostgreSQL, with the production role split.

Three login roles stand in for the three Databricks identities:
  it_jobs  (provisioning SP: owns the schema, runs jobs)
  it_req   (Data access app SP)
  it_adm   (Catalog admin app SP)
Each step runs as the identity that would perform it in production, so the test
proves both the workflow and that the grants in roles_template.sql are sufficient
and that cross-app writes are refused by the database.

Run (needs a superuser login; creates roles it_* and database cdp_security_it):
  CDP_SECURITY_IT_PG=1 PGHOST=... PGUSER=postgres PGPASSWORD=... PGSSLMODE=disable pytest tests/test_integration_pg.py
"""
import glob
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("CDP_SECURITY_IT_PG"), reason="set CDP_SECURITY_IT_PG=1 to run")

ROOT = os.path.join(os.path.dirname(__file__), "..")
DB = "cdp_security_it"
JOBS, REQ, ADM = "it_jobs", "it_req", "it_adm"


def as_role(role: str) -> None:
    from cdp_security import db
    from cdp_security.config import get_settings

    os.environ["PGUSER"] = role
    get_settings.cache_clear()
    db.reset_pool()


@pytest.fixture(scope="module")
def env():
    import psycopg

    superuser = os.environ.get("PGUSER", "postgres")
    with psycopg.connect(dbname=os.environ.get("PGDATABASE", "postgres"), user=superuser, autocommit=True) as c:
        for r in (JOBS, REQ, ADM):
            c.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{r}') "
                      f"THEN CREATE ROLE {r} LOGIN; END IF; END $$")
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB} OWNER {JOBS}")
    os.environ.update({"PGDATABASE": DB, "CDP_SECURITY_ENV": "local", "CDP_SECURITY_DRY_RUN": "true",
                       "CDP_SECURITY_GRAPH_CREATE_GROUPS": "false", "CDP_SECURITY_ADMIN_MAILBOX": "platform@x.com",
                       "CDP_SECURITY_SECURITY_MAILBOX": "sec@x.com"})
    as_role(JOBS)
    from cdp_security.db import connection
    from cdp_security.dbroles import render_grants

    with connection() as conn:
        for path in sorted(glob.glob(os.path.join(ROOT, "sql", "lakebase", "[0-9][0-9][0-9]_*.sql"))):
            with conn.transaction():
                conn.execute(open(path).read())
        template = open(os.path.join(ROOT, "sql", "lakebase", "roles_template.sql")).read()
        with conn.transaction():
            conn.execute(render_grants(template, REQ, ADM))
    yield
    as_role(superuser)


def U(email, *roles):
    from cdp_security.models import CurrentUser
    return CurrentUser(email=email, display_name=email, app_roles=frozenset(roles))


def test_end_to_end(env):
    import psycopg

    from cdp_security import dispatch, engine, lifecycle, queries, recert, service
    from cdp_security.db import fetch_all, fetch_one, transaction
    from cdp_security.models import AppRole

    admin = U("admin@x.com", AppRole.ADMIN)
    owner, steward, alice, sec = U("owner@x.com"), U("steward@x.com"), U("alice@x.com"), U("sec@x.com", AppRole.SECURITY)

    # ---- Catalog admin app registers reference data ----------------------------------
    as_role(ADM)
    ws = service.register_workspace(admin, name="dbanalytics-dev", workspace_type="DBANALYTICS",
                                    host_url="https://adb-1.azuredatabricks.net", environment="dev")
    dp = service.register_data_product(admin, code="member_contributions", name="Member contributions",
                                       description=None, catalog="dbanalytics", schema="member_contributions",
                                       classification_code="CONFIDENTIAL", owner_email=owner.email,
                                       steward_email=steward.email, home_workspace_id=ws["workspace_id"],
                                       logical_groups=["CUSTOMER", "FINANCE"])
    proj = service.register_project(admin, code="forecast_2026", name="Forecast", description=None,
                                    sponsor_email="sponsor@x.com")
    groups = {g["group_name"]: g for g in fetch_all("SELECT * FROM v_group")}
    read = groups["grp-dbx-dev-dp-member_contributions-read"]

    # ---- Data access app: requests and approvals --------------------------------------
    as_role(REQ)
    # Request app cannot change reference data or read the audit trail
    with pytest.raises(service.ServiceError, match="not permitted"):
        service.register_workspace(admin, name="sneaky", workspace_type="MLOPS",
                                   host_url="https://adb-2.azuredatabricks.net", environment="dev")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        fetch_all("SELECT * FROM audit_event")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        fetch_all("SELECT * FROM group_entitlement")

    with pytest.raises(service.ServiceError, match="project"):
        service.submit_request(alice, access_group_id=read["access_group_id"], justification="x" * 25, duration_days=30)
    with pytest.raises(service.ServiceError, match="not approved"):
        service.submit_request(alice, access_group_id=read["access_group_id"], justification="x" * 25,
                               duration_days=30, project_id=proj["project_id"])
    with pytest.raises(service.ServiceError, match="owner or an admin"):
        service.link_project_data_product(alice, proj["project_id"], dp["data_product_id"], "needed for forecasting")
    # Data owner links the project from the Data access app (My data products)
    assert [o["code"] for o in queries.owned_objects(owner.email)] == ["member_contributions"]
    service.link_project_data_product(owner, proj["project_id"], dp["data_product_id"], "needed for forecasting")

    # The admin app cannot raise requests, even valid ones (no INSERT on access_request)
    as_role(ADM)
    with pytest.raises(service.ServiceError, match="not permitted"):
        service.submit_request(admin, access_group_id=read["access_group_id"], justification="x" * 25,
                               duration_days=30, project_id=proj["project_id"])
    as_role(REQ)

    req = service.submit_request(alice, access_group_id=read["access_group_id"], justification="Monthly forecast model",
                                 duration_days=999, project_id=proj["project_id"])
    assert req["status"] == "PENDING_APPROVAL"
    assert service.tasks_for_user(alice) == [] and service.tasks_for_user(steward) == []
    [t1] = service.tasks_for_user(owner)
    service.decide_task(owner, t1["task_id"], True)
    [t2] = service.tasks_for_user(steward)
    service.decide_task(steward, t2["task_id"], True)
    grant = fetch_one("SELECT * FROM access_grant WHERE request_id = %s", (req["request_id"],))
    assert grant["status"] == "PENDING_PROVISION" and (grant["valid_to"] - grant["valid_from"]).days == 180

    # ---- Jobs: provisioning defers until the group exists ------------------------------
    as_role(JOBS)
    assert engine.run(engine.Providers(dry_run=True))["deferred"] >= 1
    as_role(ADM)
    service.set_group_object_id(admin, read["access_group_id"], "11111111-aaaa-bbbb-cccc-000000000001")
    as_role(JOBS)
    with transaction() as cur:
        cur.execute("UPDATE provisioning_action SET next_attempt_at = now()")
    engine.run(engine.Providers(dry_run=True))
    assert fetch_one("SELECT status FROM access_grant WHERE grant_id = %s", (grant["grant_id"],))["status"] == "ACTIVE"
    assert fetch_one("SELECT status FROM access_request WHERE request_id = %s", (req["request_id"],))["status"] == "FULFILLED"

    # ---- Data access app: extension, rejection, break-glass ---------------------------
    as_role(REQ)
    assert [g["user_email"] for g in queries.grants_for_owned(owner.email)] == ["alice@x.com"]
    ext = service.request_extension(alice, grant["grant_id"], 30, "Forecast runs into next quarter")
    for approver in (owner, steward):
        [t] = service.tasks_for_user(approver)
        service.decide_task(approver, t["task_id"], True)
    assert fetch_one("SELECT status FROM access_request WHERE request_id = %s", (ext["request_id"],))["status"] == "FULFILLED"

    bob = U("bob@x.com")
    r2 = service.submit_request(bob, access_group_id=read["access_group_id"], justification="Ad hoc analysis please",
                                duration_days=10, project_id=proj["project_id"])
    [t] = service.tasks_for_user(owner)
    service.decide_task(owner, t["task_id"], False, "Use the aggregated product instead")
    assert fetch_one("SELECT status FROM access_request WHERE request_id = %s", (r2["request_id"],))["status"] == "REJECTED"

    oncall = U("oncall@x.com", AppRole.BREAK_GLASS)
    bg = service.break_glass(oncall, read["access_group_id"], "INC0001", "Production incident on contributions feed")
    as_role(JOBS)
    engine.run(engine.Providers(dry_run=True))
    as_role(REQ)
    [review] = service.tasks_for_user(sec)
    service.decide_task(sec, review["task_id"], False, "No incident linked to this data")
    as_role(JOBS)
    engine.run(engine.Providers(dry_run=True))
    g = fetch_one("SELECT status, revoke_reason FROM access_grant WHERE request_id = %s", (bg["request_id"],))
    assert g == {"status": "REVOKED", "revoke_reason": "POST_HOC_REJECTED"}

    # ---- Recertification: owner keeps one decision; unattested is revoked ---------------
    with transaction() as cur:
        cur.execute("UPDATE access_grant SET last_recertified_at = now() - interval '200 days' WHERE grant_id = %s",
                    (grant["grant_id"],))
    assert recert.open_periodic() == 1
    as_role(REQ)
    assert len(queries.recert_items_for(owner.email)) == 1
    as_role(JOBS)
    with transaction() as cur:
        cur.execute("UPDATE recert_campaign SET due_at = now() - interval '1 minute'")
    assert recert.close_overdue() == 1
    engine.run(engine.Providers(dry_run=True))
    assert fetch_one("SELECT status FROM access_grant WHERE grant_id = %s", (grant["grant_id"],))["status"] == "REVOKED"

    # ---- Escalation and lifecycle (jobs) ---------------------------------------------
    as_role(REQ)
    r3 = service.submit_request(bob, access_group_id=read["access_group_id"], justification="Second try with project",
                                duration_days=10, project_id=proj["project_id"])
    as_role(JOBS)
    with transaction() as cur:
        cur.execute("UPDATE approval_task SET due_at = now() - interval '1 hour' WHERE request_id = %s",
                    (r3["request_id"],))
    assert lifecycle.escalate_overdue_tasks()["escalated"] == 1
    lifecycle.expire_grants()
    lifecycle.send_expiry_notices()

    # ---- Every page query works under its app's role -----------------------------------
    as_role(REQ)
    for fn, a in [(queries.requestable_groups, ()), (queries.projects_for_group, (read,)),
                  (queries.my_grants, (alice.email,)), (queries.my_requests, (alice.email,)),
                  (queries.my_decisions, (owner.email,)), (queries.request_tasks, (req["request_id"],)),
                  (queries.recert_items_for, (owner.email,)), (queries.delegations_for, (owner.email,)),
                  (queries.owned_objects, (owner.email,)), (queries.grants_for_owned, (owner.email,)),
                  (queries.project_links, ()), (queries.projects, ()), (queries.classifications, ()),
                  (queries.approval_rules, ()), (queries.active_groups, ())]:
        fn(*a)
    from datetime import datetime, timedelta, timezone
    service.add_delegation(owner, owner.email, "cover@x.com", datetime.now(timezone.utc) + timedelta(days=10),
                           "Annual leave")
    as_role(ADM)
    now = datetime.now(timezone.utc)
    for fn, a in [(queries.workspaces, ()), (queries.data_products, ()), (queries.projects, ()),
                  (queries.project_links, ()), (queries.assets, ()), (queries.ai_use_cases, ()),
                  (queries.groups, ()), (queries.entitlements, (read["access_group_id"],)),
                  (queries.ownerships, ()), (queries.app_role_members, ()), (queries.logical_groups, ()),
                  (queries.actions, (["DEAD", "QUEUED"],)), (queries.queue_summary, ()),
                  (queries.failed_notifications, ()), (queries.audit_events, (now - timedelta(days=1), now, None, None)),
                  (queries.drift_findings, (["OPEN"],)), (queries.last_drift_run, ()), (queries.access_evidence, ()),
                  (queries.group_members, (read["access_group_id"],))]:
        fn(*a)
    service.set_app_role(admin, "auditor@x.com", AppRole.AUDITOR, True)
    service.set_app_role(admin, "auditor@x.com", AppRole.AUDITOR, False)

    # ---- Audit trail: one unbroken chain across both apps and the jobs ------------------
    assert service.verify_audit_chain() == []
    actors = {r["actor"] for r in fetch_all("SELECT DISTINCT actor FROM audit_event")}
    assert {"admin@x.com", "owner@x.com", "alice@x.com"} <= actors
    as_role(JOBS)
    with pytest.raises(Exception, match="append-only"):
        with transaction() as cur:
            cur.execute("DELETE FROM audit_event")
    assert dispatch.dispatch(dry_run=True)["sent"] > 0
