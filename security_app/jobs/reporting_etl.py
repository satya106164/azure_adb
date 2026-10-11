"""Daily: refresh the reporting star schema from the operational Delta tables.

Everything is in one schema, so this is plain SQL on the warehouse. SCD1
dimensions are views (always current). dim_user is SCD2 (MERGE); facts are
rebuilt (small volumes); the periodic snapshot replaces today's partition.
The audit trail needs no copy: audit_event is already an append-only Delta table.
"""
import os
import sys

# Serverless spark_python_task exec()s the file, so __file__ is unset; fall back to --bundle-root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
                else os.path.join(sys.argv[sys.argv.index("--bundle-root") + 1], "jobs"))

from _common import bootstrap

args = bootstrap()
from cdp_security.config import get_settings  # noqa: E402
from cdp_security.db import execute, fetch_one  # noqa: E402

RECERT_INTERVAL_DAYS = int(get_settings().recert_interval_days)


def dk(col: str) -> str:
    return f"CAST(date_format({col}, 'yyyyMMdd') AS INT)"


def uj(alias: str, email_col: str, at_col: str) -> str:
    """Point-in-time LEFT JOIN to the SCD2 user dimension (version valid at the event time)."""
    at = f"coalesce({at_col}, current_timestamp())"
    return (f"LEFT JOIN dim_user {alias} ON {alias}.email = {email_col} "
            f"AND {at} >= {alias}.effective_from AND {at} < {alias}.effective_to")


# ---- dim_date (once) -----------------------------------------------------------
if fetch_one("SELECT count(*) AS n FROM dim_date")["n"] == 0:
    execute("""
        INSERT INTO dim_date
        SELECT CAST(date_format(d, 'yyyyMMdd') AS INT), d, year(d), quarter(d), month(d), date_format(d, 'MMMM'),
               dayofweek(d), dayofweek(d) IN (1, 7),
               CASE WHEN d >= make_date(year(d), 4, 6) THEN concat(year(d), '/', right(year(d) + 1, 2))
                    ELSE concat(year(d) - 1, '/', right(year(d), 2)) END
        FROM (SELECT explode(sequence(DATE'2020-01-01', DATE'2040-12-31', INTERVAL 1 DAY)) AS d)""")

# ---- dim_user SCD2 ------------------------------------------------------------------
execute("""
    MERGE INTO dim_user t
    USING (
      WITH src AS (
        SELECT email, display_name, department, is_active, created_at,
               sha2(concat_ws('|', email, coalesce(display_name, ''), coalesce(department, ''),
                              CAST(is_active AS STRING)), 256) AS row_hash
        FROM app_user)
      SELECT s.email AS merge_key, s.* FROM src s
      UNION ALL
      SELECT NULL AS merge_key, s.* FROM src s
      JOIN dim_user d ON d.email = s.email AND d.is_current AND d.row_hash <> s.row_hash
    ) u
    ON t.email = u.merge_key AND t.is_current
    WHEN MATCHED AND t.row_hash <> u.row_hash THEN
      UPDATE SET is_current = false, effective_to = current_timestamp()
    WHEN NOT MATCHED THEN INSERT (email, display_name, department, is_active, row_hash,
                                  effective_from, effective_to, is_current)
      VALUES (u.email, u.display_name, u.department, u.is_active, u.row_hash,
              CASE WHEN u.merge_key IS NULL THEN current_timestamp() ELSE u.created_at END,
              TIMESTAMP'9999-12-31 00:00:00', true)""")

# ---- facts ------------------------------------------------------------------------
execute(f"""
    INSERT OVERWRITE fact_access_request
    SELECT r.request_id, r.request_ref, r.request_type, rq.user_sk, bn.user_sk, r.group_code,
           g.data_product_code, r.classification_code, r.status,
           {dk('r.submitted_at')}, {dk('r.decided_at')}, {dk('r.fulfilled_at')},
           r.submitted_at, r.decided_at, r.fulfilled_at,
           (unix_timestamp(r.decided_at) - unix_timestamp(r.submitted_at)) / 3600.0,
           (unix_timestamp(r.fulfilled_at) - unix_timestamp(r.submitted_at)) / 3600.0,
           CAST(t.stage_count AS INT), coalesce(t.any_breach, false), r.requested_duration_days
    FROM access_request r
    JOIN access_group g ON g.group_code = r.group_code
    {uj('rq', 'r.requester_email', 'r.submitted_at')}
    {uj('bn', 'r.beneficiary_email', 'r.submitted_at')}
    LEFT JOIN (SELECT request_id, count(*) AS stage_count,
                      max(coalesce(decided_at, current_timestamp()) > due_at) AS any_breach
               FROM approval_task GROUP BY request_id) t ON t.request_id = r.request_id""")
execute(f"""
    INSERT OVERWRITE fact_approval_event
    SELECT t.task_id, t.request_id, t.stage_no, t.approver_role, t.status, dby.user_sk, obo.user_sk,
           t.created_at, t.due_at, t.decided_at, {dk('t.decided_at')},
           (unix_timestamp(t.decided_at) - unix_timestamp(t.created_at)) / 3600.0,
           coalesce(t.decided_at, current_timestamp()) > t.due_at, t.escalated_at IS NOT NULL
    FROM approval_task t
    {uj('dby', 't.decided_by_email', 't.decided_at')}
    {uj('obo', 't.on_behalf_of_email', 't.decided_at')}""")
execute(f"""
    INSERT OVERWRITE fact_grant_lifecycle
    SELECT gr.grant_id, gr.request_id, gu.user_sk, gr.group_code, g.data_product_code, r.classification_code,
           gr.status, r.submitted_at, r.decided_at, gr.provisioned_at, gr.valid_to,
           gr.last_recertified_at, gr.revoke_requested_at, gr.revoked_at, gr.revoke_reason,
           {dk('gr.provisioned_at')}, {dk('gr.revoked_at')},
           (unix_timestamp(gr.provisioned_at) - unix_timestamp(r.decided_at)) / 3600.0,
           (unix_timestamp(gr.revoked_at) - unix_timestamp(gr.revoke_requested_at)) / 3600.0,
           datediff(coalesce(gr.revoked_at, current_timestamp()), gr.provisioned_at)
    FROM access_grant gr
    JOIN access_request r ON r.request_id = gr.request_id
    JOIN access_group g ON g.group_code = gr.group_code
    {uj('gu', 'gr.user_email', 'gr.valid_from')}""")
execute(f"""
    INSERT INTO fact_access_snapshot REPLACE WHERE snapshot_date = current_date()
    SELECT current_date(), {dk('current_date()')}, gr.grant_id, su.user_sk, gr.group_code, g.data_product_code,
           CAST(NULL AS STRING), datediff(gr.valid_to, current_date()),
           datediff(current_date(), coalesce(gr.last_recertified_at, gr.provisioned_at, gr.valid_from)),
           datediff(current_date(), coalesce(gr.last_recertified_at, gr.provisioned_at, gr.valid_from))
             > {RECERT_INTERVAL_DAYS}
    FROM access_grant gr
    JOIN v_group g ON g.group_code = gr.group_code
    {uj('su', 'gr.user_email', 'current_timestamp()')}
    WHERE gr.status IN ('ACTIVE', 'PENDING_REVOKE')""")
print("Reporting ETL complete")
