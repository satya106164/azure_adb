"""Daily: build the reporting star schema in Delta from the operational store.

Reads the Lakebase tables directly over Postgres (OAuth, like every other job)
into Spark temp views named ops_<table>, so no Unity Catalog catalog is needed
over Lakebase. Dimensions are SCD1 overwrites except dim_user (SCD2 MERGE);
facts are rebuilt (small volumes); the periodic snapshot replaces today's
partition; the audit archive is appended incrementally by chain_seq.
"""
import json
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap, spark_session

args = bootstrap()
if not args.reporting_catalog:
    raise SystemExit("--reporting-catalog is required")

from pyspark.sql import types as T  # noqa: E402

from cdp_security.db import connection  # noqa: E402

spark = spark_session()
RPT = f"`{args.reporting_catalog}`.`{args.reporting_schema}`"

# Postgres type OID -> Spark type; anything else (text, uuid, jsonb, interval) is loaded as a string
_PG_TYPES = {16: T.BooleanType(), 21: T.IntegerType(), 23: T.IntegerType(), 20: T.LongType(),
             700: T.DoubleType(), 701: T.DoubleType(), 1700: T.DoubleType(),
             1082: T.DateType(), 1114: T.TimestampType(), 1184: T.TimestampType()}


def _text(v):
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str, sort_keys=True)
    return str(v)  # uuid, interval, ...


def load_ops(table: str, where: str = "") -> None:
    """Copy one Lakebase table (cdp_security schema) into the temp view ops_<table>."""
    with connection() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table} {where}")  # table names are literals below, never input
        cols = [(d.name, _PG_TYPES.get(d.type_code, T.StringType())) for d in cur.description]
        rows = [tuple(r[name] if not isinstance(t, T.StringType) else _text(r[name]) for name, t in cols)
                for r in cur.fetchall()]
    schema = T.StructType([T.StructField(name, t, True) for name, t in cols])
    spark.createDataFrame(rows, schema).createOrReplaceTempView(f"ops_{table}")


for _t in ("classification", "workspace", "project", "app_user", "data_product", "uc_securable", "access_group",
           "ai_use_case", "asset", "project_data_product", "group_entitlement", "securable_logical_group",
           "access_request", "approval_task", "access_grant", "drift_finding"):
    load_ops(_t)


def sql(stmt: str):
    return spark.sql(stmt.replace("{OPS}.", "ops_").replace("{RPT}", RPT))


def dk(col: str) -> str:
    return f"CAST(date_format({col}, 'yyyyMMdd') AS INT)"


# ---- dim_date (once) -----------------------------------------------------------
if sql("SELECT count(*) AS n FROM {RPT}.dim_date").first()["n"] == 0:
    sql("""
        INSERT INTO {RPT}.dim_date
        SELECT CAST(date_format(d, 'yyyyMMdd') AS INT), d, year(d), quarter(d), month(d), date_format(d, 'MMMM'),
               dayofweek(d), dayofweek(d) IN (1, 7),
               CASE WHEN d >= make_date(year(d), 4, 6) THEN concat(year(d), '/', right(year(d) + 1, 2))
                    ELSE concat(year(d) - 1, '/', right(year(d), 2)) END
        FROM (SELECT explode(sequence(DATE'2020-01-01', DATE'2040-12-31', INTERVAL 1 DAY)) AS d)""")

# ---- SCD1 dimensions and bridges ---------------------------------------------------
sql("INSERT OVERWRITE {RPT}.dim_classification SELECT classification_code, rank, max_grant_days, "
    "recert_interval_days, requires_project, description FROM {OPS}.classification")
sql("""
    INSERT OVERWRITE {RPT}.dim_status VALUES
      ('REQUEST','DRAFT','open',false), ('REQUEST','PENDING_APPROVAL','open',false),
      ('REQUEST','APPROVED','in progress',false), ('REQUEST','PROVISIONING','in progress',false),
      ('REQUEST','PROVISIONING_FAILED','exception',false), ('REQUEST','FULFILLED','closed',true),
      ('REQUEST','REJECTED','closed',true), ('REQUEST','CANCELLED','closed',true),
      ('GRANT','PENDING_PROVISION','in progress',false), ('GRANT','ACTIVE','live',false),
      ('GRANT','PENDING_REVOKE','in progress',false), ('GRANT','REVOKED','ended',true),
      ('GRANT','EXPIRED','ended',true), ('GRANT','FAILED','exception',true),
      ('TASK','PENDING','open',false), ('TASK','APPROVED','decided',true), ('TASK','REJECTED','decided',true),
      ('TASK','CANCELLED','closed',true), ('TASK','EXPIRED','closed',true)""")
sql("INSERT OVERWRITE {RPT}.dim_workspace SELECT CAST(workspace_id AS STRING), name, workspace_type, environment, "
    "host_url, is_active FROM {OPS}.workspace")
sql("""INSERT OVERWRITE {RPT}.dim_project
       SELECT CAST(p.project_id AS STRING), p.code, p.name, u.email, CAST(p.workspace_id AS STRING),
              p.start_date, p.end_date, p.status
       FROM {OPS}.project p JOIN {OPS}.app_user u ON u.user_id = p.sponsor_user_id""")
sql("""INSERT OVERWRITE {RPT}.dim_data_product
       SELECT CAST(data_product_id AS STRING), code, name, catalog_name, schema_name, classification_code,
              lifecycle_status, CAST(home_workspace_id AS STRING) FROM {OPS}.data_product""")
sql("""INSERT OVERWRITE {RPT}.dim_schema
       SELECT CAST(securable_id AS STRING), securable_type, full_name, split(full_name, '\\\\.')[0],
              CAST(data_product_id AS STRING), classification_code FROM {OPS}.uc_securable""")
sql("""INSERT OVERWRITE {RPT}.dim_access_group
       SELECT CAST(g.access_group_id AS STRING), g.group_name, g.purpose, g.privilege_level, g.classification_code,
              g.status, g.requestable, CAST(coalesce(g.data_product_id, uc.data_product_id) AS STRING),
              CAST(g.project_id AS STRING), uc.code, u.email
       FROM {OPS}.access_group g
       LEFT JOIN {OPS}.ai_use_case uc ON uc.ai_use_case_id = g.ai_use_case_id
       LEFT JOIN {OPS}.app_user u ON u.user_id = g.owner_user_id""")
sql("""INSERT OVERWRITE {RPT}.dim_asset
       SELECT CAST(a.asset_id AS STRING), a.asset_type, a.name, a.external_id, CAST(a.workspace_id AS STRING),
              CAST(a.data_product_id AS STRING), uc.code, a.classification_code, a.is_active
       FROM {OPS}.asset a LEFT JOIN {OPS}.ai_use_case uc ON uc.ai_use_case_id = a.ai_use_case_id""")
sql("""INSERT OVERWRITE {RPT}.bridge_project_data_product
       SELECT CAST(project_id AS STRING), CAST(data_product_id AS STRING), linked_by, linked_at
       FROM {OPS}.project_data_product""")
sql("""INSERT OVERWRITE {RPT}.bridge_group_entitlement
       SELECT CAST(access_group_id AS STRING), CAST(securable_id AS STRING), CAST(asset_id AS STRING), privilege,
              desired_state, apply_status FROM {OPS}.group_entitlement""")
sql("""INSERT OVERWRITE {RPT}.bridge_schema_logical_group
       SELECT CAST(securable_id AS STRING), logical_group_code FROM {OPS}.securable_logical_group""")

# ---- dim_user SCD2 ------------------------------------------------------------------
sql("""
    CREATE OR REPLACE TEMP VIEW src_user AS
    SELECT CAST(user_id AS STRING) AS user_id, email, display_name, department, is_active, created_at,
           sha2(concat_ws('|', email, coalesce(display_name, ''), coalesce(department, ''),
                          CAST(is_active AS STRING)), 256) AS row_hash
    FROM {OPS}.app_user""")
sql("""
    MERGE INTO {RPT}.dim_user t
    USING (
      SELECT s.user_id AS merge_key, s.* FROM src_user s
      UNION ALL
      SELECT NULL AS merge_key, s.* FROM src_user s
      JOIN {RPT}.dim_user d ON d.user_id = s.user_id AND d.is_current AND d.row_hash <> s.row_hash
    ) u
    ON t.user_id = u.merge_key AND t.is_current
    WHEN MATCHED AND t.row_hash <> u.row_hash THEN
      UPDATE SET is_current = false, effective_to = current_timestamp()
    WHEN NOT MATCHED THEN INSERT (user_id, email, display_name, department, is_active, row_hash,
                                  effective_from, effective_to, is_current)
      VALUES (u.user_id, u.email, u.display_name, u.department, u.is_active, u.row_hash,
              CASE WHEN u.merge_key IS NULL THEN current_timestamp() ELSE u.created_at END,
              TIMESTAMP'9999-12-31 00:00:00', true)""")


def uj(alias: str, user_id_col: str, at_col: str) -> str:
    """Point-in-time LEFT JOIN to the SCD2 user dimension (version valid at the event time)."""
    at = f"coalesce({at_col}, current_timestamp())"
    return (f"LEFT JOIN {RPT}.dim_user {alias} ON {alias}.user_id = CAST({user_id_col} AS STRING) "
            f"AND {at} >= {alias}.effective_from AND {at} < {alias}.effective_to")


# ---- facts ------------------------------------------------------------------------
sql(f"""
    INSERT OVERWRITE {{RPT}}.fact_access_request
    SELECT CAST(r.request_id AS STRING), r.request_ref, r.request_type,
           rq.user_sk, bn.user_sk,
           CAST(r.access_group_id AS STRING), CAST(r.project_id AS STRING),
           CAST(coalesce(g.data_product_id, uc.data_product_id) AS STRING), r.classification_code, r.status,
           {dk('r.submitted_at')}, {dk('r.decided_at')}, {dk('r.fulfilled_at')},
           r.submitted_at, r.decided_at, r.fulfilled_at,
           (unix_timestamp(r.decided_at) - unix_timestamp(r.submitted_at)) / 3600.0,
           (unix_timestamp(r.fulfilled_at) - unix_timestamp(r.submitted_at)) / 3600.0,
           CAST(t.stage_count AS INT), coalesce(t.any_breach, false), r.requested_duration_days
    FROM {{OPS}}.access_request r
    JOIN {{OPS}}.access_group g ON g.access_group_id = r.access_group_id
    LEFT JOIN {{OPS}}.ai_use_case uc ON uc.ai_use_case_id = g.ai_use_case_id
    {uj('rq', 'r.requester_user_id', 'r.submitted_at')}
    {uj('bn', 'r.beneficiary_user_id', 'r.submitted_at')}
    LEFT JOIN (SELECT request_id, count(*) AS stage_count,
                      max(coalesce(decided_at, current_timestamp()) > due_at) AS any_breach
               FROM {{OPS}}.approval_task GROUP BY request_id) t ON t.request_id = r.request_id
""")
sql(f"""
    INSERT OVERWRITE {{RPT}}.fact_approval_event
    SELECT CAST(t.task_id AS STRING), CAST(t.request_id AS STRING), t.stage_no, t.approver_role, t.status,
           t.is_post_hoc, dby.user_sk, obo.user_sk,
           t.created_at, t.due_at, t.decided_at, {dk('t.decided_at')},
           (unix_timestamp(t.decided_at) - unix_timestamp(t.created_at)) / 3600.0,
           coalesce(t.decided_at, current_timestamp()) > t.due_at, t.escalated_at IS NOT NULL
    FROM {{OPS}}.approval_task t
    {uj('dby', 't.decided_by_user_id', 't.decided_at')}
    {uj('obo', 't.on_behalf_of_user_id', 't.decided_at')}
""")
sql(f"""
    INSERT OVERWRITE {{RPT}}.fact_grant_lifecycle
    SELECT CAST(gr.grant_id AS STRING), CAST(gr.request_id AS STRING), gu.user_sk,
           CAST(gr.access_group_id AS STRING), CAST(coalesce(g.data_product_id, uc.data_product_id) AS STRING),
           g.classification_code, gr.status, gr.is_break_glass,
           r.submitted_at, r.decided_at, gr.provisioned_at, gr.valid_to, gr.last_recertified_at,
           gr.revoke_requested_at, gr.revoked_at, gr.revoke_reason,
           {dk('gr.provisioned_at')}, {dk('gr.revoked_at')},
           (unix_timestamp(gr.provisioned_at) - unix_timestamp(r.decided_at)) / 3600.0,
           (unix_timestamp(gr.revoked_at) - unix_timestamp(gr.revoke_requested_at)) / 3600.0,
           datediff(coalesce(gr.revoked_at, current_timestamp()), gr.provisioned_at)
    FROM {{OPS}}.access_grant gr
    JOIN {{OPS}}.access_request r ON r.request_id = gr.request_id
    JOIN {{OPS}}.access_group g ON g.access_group_id = gr.access_group_id
    LEFT JOIN {{OPS}}.ai_use_case uc ON uc.ai_use_case_id = g.ai_use_case_id
    {uj('gu', 'gr.user_id', 'gr.valid_from')}
""")
sql(f"""
    INSERT INTO {{RPT}}.fact_access_snapshot REPLACE WHERE snapshot_date = current_date()
    SELECT current_date(), {dk('current_date()')}, CAST(gr.grant_id AS STRING),
           su.user_sk, CAST(gr.access_group_id AS STRING),
           CAST(coalesce(g.data_product_id, uc.data_product_id) AS STRING), g.classification_code,
           gr.is_break_glass, datediff(gr.valid_to, current_date()),
           datediff(current_date(), coalesce(gr.last_recertified_at, gr.provisioned_at, gr.valid_from)),
           datediff(current_date(), coalesce(gr.last_recertified_at, gr.provisioned_at, gr.valid_from))
             > c.recert_interval_days
    FROM {{OPS}}.access_grant gr
    JOIN {{OPS}}.access_group g ON g.access_group_id = gr.access_group_id
    JOIN {{OPS}}.classification c ON c.classification_code = g.classification_code
    LEFT JOIN {{OPS}}.ai_use_case uc ON uc.ai_use_case_id = g.ai_use_case_id
    {uj('su', 'gr.user_id', 'current_timestamp()')}
    WHERE gr.status IN ('ACTIVE', 'PENDING_REVOKE')
""")

# ---- audit archive (incremental, append-only) -------------------------------------------
last = sql("SELECT coalesce(max(chain_seq), 0) AS n FROM {RPT}.audit_event_archive").first()["n"]
load_ops("audit_event", f"WHERE chain_seq > {int(last)}")
sql(f"""
    INSERT INTO {{RPT}}.audit_event_archive
    SELECT chain_seq, occurred_at, actor, actor_type, action, object_kind, object_id, correlation_id,
           CAST(before_state AS STRING), CAST(after_state AS STRING), reason, prev_hash, event_hash
    FROM {{OPS}}.audit_event WHERE chain_seq > {int(last)}
""")
sql("""INSERT OVERWRITE {RPT}.drift_finding_history
       SELECT CAST(finding_id AS STRING), CAST(run_id AS STRING), finding_type, severity, securable, principal,
              privilege, status, first_detected_at, last_detected_at, resolved_at, resolved_by, resolution_note
       FROM {OPS}.drift_finding""")
print("Reporting ETL complete")
