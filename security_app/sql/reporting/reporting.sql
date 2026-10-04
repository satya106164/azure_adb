-- =============================================================================
-- Reporting / audit star schema, in the same schema as the operational tables.
-- Re-applied by jobs/migrate.py on every run: tables are CREATE IF NOT EXISTS,
-- views are CREATE OR REPLACE. Filled daily by jobs/reporting_etl.py.
-- Readers: auditors, security, platform admins only (contains user emails).
--
-- SCD1 dimensions are views over the operational tables (always current).
-- dim_user is SCD2 and the facts are tables, so reports can join the version
-- of a person that was valid when the event happened.
-- =============================================================================

-- ---------------- Dimensions ----------------
CREATE TABLE IF NOT EXISTS dim_date (
  date_key INT NOT NULL, calendar_date DATE NOT NULL, year INT, quarter INT, month INT, month_name STRING,
  day_of_week INT, is_weekend BOOLEAN, uk_fiscal_year STRING
) COMMENT 'Calendar; UK fiscal year starts 6 April';

CREATE TABLE IF NOT EXISTS dim_user (
  user_sk BIGINT GENERATED ALWAYS AS IDENTITY,
  email STRING NOT NULL, display_name STRING, department STRING, is_active BOOLEAN,
  row_hash STRING, effective_from TIMESTAMP, effective_to TIMESTAMP, is_current BOOLEAN
) COMMENT 'SCD2: one row per version of a user (department / status changes)';

CREATE OR REPLACE VIEW dim_classification AS
SELECT classification_code, rank, max_grant_days, recert_interval_days, description FROM classification;

CREATE OR REPLACE VIEW dim_status AS
SELECT * FROM VALUES
  ('REQUEST','DRAFT','open',false), ('REQUEST','PENDING_APPROVAL','open',false),
  ('REQUEST','APPROVED','awaiting admin',false), ('REQUEST','FULFILLED','closed',true),
  ('REQUEST','REJECTED','closed',true), ('REQUEST','CANCELLED','closed',true),
  ('GRANT','PENDING_PROVISION','awaiting admin',false), ('GRANT','ACTIVE','live',false),
  ('GRANT','PENDING_REVOKE','awaiting admin',false), ('GRANT','REVOKED','ended',true),
  ('GRANT','EXPIRED','ended',true),
  ('TASK','PENDING','open',false), ('TASK','APPROVED','decided',true), ('TASK','REJECTED','decided',true),
  ('TASK','CANCELLED','closed',true), ('TASK','EXPIRED','closed',true)
  AS t(status_domain, status_code, status_group, is_terminal);

CREATE OR REPLACE VIEW dim_workspace AS
SELECT workspace_code, name, workspace_type, environment, host_url, databricks_workspace_id, is_active
FROM workspace;

CREATE OR REPLACE VIEW dim_data_product AS
SELECT data_product_code, name, data_product_type, project_name, catalog_name, schema_name,
       concat(catalog_name, '.', schema_name) AS full_name,
       classification_code, lifecycle_status, home_workspace_code, owner_email, steward_email
FROM data_product;

CREATE OR REPLACE VIEW dim_access_group AS
SELECT group_code, group_name, data_product_code, classification_code, status, requestable
FROM v_group;

DROP VIEW IF EXISTS bridge_group_entitlement;

-- ---------------- Facts ----------------
CREATE TABLE IF NOT EXISTS fact_access_request (
  request_id STRING NOT NULL, request_ref STRING, request_type STRING,
  requester_user_sk BIGINT, beneficiary_user_sk BIGINT, group_code STRING,
  data_product_code STRING, classification_code STRING, status_code STRING,
  submitted_date_key INT, decided_date_key INT, fulfilled_date_key INT,
  submitted_at TIMESTAMP, decided_at TIMESTAMP, fulfilled_at TIMESTAMP,
  hours_to_decision DOUBLE, hours_to_fulfil DOUBLE, stage_count INT, any_sla_breach BOOLEAN,
  requested_duration_days INT)
COMMENT 'Grain: one row per access request';

CREATE TABLE IF NOT EXISTS fact_approval_event (
  task_id STRING NOT NULL, request_id STRING, stage_no INT, approver_role STRING, status_code STRING,
  is_post_hoc BOOLEAN, decided_by_user_sk BIGINT, on_behalf_of_user_sk BIGINT, created_at TIMESTAMP,
  due_at TIMESTAMP, decided_at TIMESTAMP, decided_date_key INT, hours_to_decide DOUBLE, sla_breached BOOLEAN,
  escalated BOOLEAN)
COMMENT 'Grain: one row per approval task (stage) of a request';

CREATE TABLE IF NOT EXISTS fact_grant_lifecycle (
  grant_id STRING NOT NULL, request_id STRING, user_sk BIGINT, group_code STRING, data_product_code STRING,
  classification_code STRING, status_code STRING, is_break_glass BOOLEAN,
  requested_at TIMESTAMP, approved_at TIMESTAMP, provisioned_at TIMESTAMP, valid_to TIMESTAMP,
  last_recertified_at TIMESTAMP, revoke_requested_at TIMESTAMP, revoked_at TIMESTAMP, revoke_reason STRING,
  provisioned_date_key INT, revoked_date_key INT, hours_approval_to_provision DOUBLE,
  hours_revoke_to_removed DOUBLE, days_active INT)
COMMENT 'Accumulating snapshot. Grain: one row per grant, milestones filled in as they happen';

CREATE TABLE IF NOT EXISTS fact_access_snapshot (
  snapshot_date DATE NOT NULL, snapshot_date_key INT, grant_id STRING NOT NULL, user_sk BIGINT,
  group_code STRING, data_product_code STRING, classification_code STRING, is_break_glass BOOLEAN,
  days_to_expiry INT, days_since_recert INT, recert_overdue BOOLEAN)
PARTITIONED BY (snapshot_date)
COMMENT 'Periodic snapshot. Grain: one row per live grant per day';

-- ---------------- Business views ----------------
CREATE OR REPLACE VIEW v_open_requests AS
SELECT f.request_ref, f.request_type, f.status_code, f.classification_code, g.group_name,
       u.email AS beneficiary_email, f.submitted_at, f.any_sla_breach
FROM fact_access_request f
JOIN dim_access_group g ON g.group_code = f.group_code
LEFT JOIN dim_user u ON u.user_sk = f.beneficiary_user_sk
WHERE f.status_code IN ('PENDING_APPROVAL', 'APPROVED');

CREATE OR REPLACE VIEW v_sla_performance AS
SELECT date_trunc('MONTH', decided_at) AS month, approver_role, count(*) AS decisions,
       avg(hours_to_decide) AS avg_hours, sum(CASE WHEN sla_breached THEN 1 ELSE 0 END) AS breaches
FROM fact_approval_event
WHERE decided_at IS NOT NULL
GROUP BY 1, 2;

CREATE OR REPLACE VIEW v_access_by_classification AS
SELECT s.snapshot_date, s.classification_code, s.data_product_code, count(*) AS live_grants,
       sum(CASE WHEN s.recert_overdue THEN 1 ELSE 0 END) AS overdue_recert,
       sum(CASE WHEN s.is_break_glass THEN 1 ELSE 0 END) AS break_glass
FROM fact_access_snapshot s
GROUP BY 1, 2, 3;

CREATE OR REPLACE VIEW v_who_has_access_now AS
SELECT u.email, u.department, g.group_name, g.classification_code, s.data_product_code,
       s.days_to_expiry, s.days_since_recert, s.is_break_glass
FROM fact_access_snapshot s
JOIN dim_user u ON u.user_sk = s.user_sk
JOIN dim_access_group g ON g.group_code = s.group_code
WHERE s.snapshot_date = (SELECT max(snapshot_date) FROM fact_access_snapshot);

-- Manual changes: who added / removed each member by hand, and how long it took after approval
CREATE OR REPLACE VIEW v_manual_fulfilment AS
SELECT request_ref, request_type, user_email, group_name, data_product_code, data_product_name, data_product_type,
       classification_code, status, is_break_glass,
       decided_at AS approved_at, provisioned_at AS granted_at, provisioned_by AS granted_by, fulfilment_note,
       (unix_timestamp(provisioned_at) - unix_timestamp(decided_at)) / 3600.0 AS hours_to_grant,
       revoke_requested_at, revoke_reason, revoked_at AS removed_at, removed_by, removal_note,
       (unix_timestamp(revoked_at) - unix_timestamp(revoke_requested_at)) / 3600.0 AS hours_to_remove
FROM v_grant;
