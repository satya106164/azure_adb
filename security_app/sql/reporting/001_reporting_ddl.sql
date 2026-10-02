-- =============================================================================
-- Reporting / audit star schema (Delta, Unity Catalog).
-- ${catalog}.${schema} is substituted by jobs/migrate.py. Idempotent.
-- Readers: auditors, security, platform admins only (contains user emails).
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS ${catalog}.${schema}
COMMENT 'Security access workflow - dimensional model for reporting and audit';

-- ---------------- Dimensions ----------------
CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_date (
  date_key INT NOT NULL, calendar_date DATE NOT NULL, year INT, quarter INT, month INT, month_name STRING,
  day_of_week INT, is_weekend BOOLEAN, uk_fiscal_year STRING
) COMMENT 'Calendar; UK fiscal year starts 6 April';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_user (
  user_sk BIGINT GENERATED ALWAYS AS IDENTITY,
  user_id STRING NOT NULL, email STRING, display_name STRING, department STRING, is_active BOOLEAN,
  row_hash STRING, effective_from TIMESTAMP, effective_to TIMESTAMP, is_current BOOLEAN
) COMMENT 'SCD2: one row per version of a user (department / status changes)';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_classification (
  classification_code STRING NOT NULL, rank INT, max_grant_days INT, recert_interval_days INT,
  requires_project BOOLEAN, description STRING);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_status (
  status_domain STRING NOT NULL, status_code STRING NOT NULL, status_group STRING, is_terminal BOOLEAN)
COMMENT 'Statuses of requests, grants and approval tasks';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_workspace (
  workspace_id STRING NOT NULL, name STRING, workspace_type STRING, environment STRING, host_url STRING,
  is_active BOOLEAN);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_project (
  project_id STRING NOT NULL, code STRING, name STRING, sponsor_email STRING, workspace_id STRING,
  start_date DATE, end_date DATE, status STRING);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_data_product (
  data_product_id STRING NOT NULL, code STRING, name STRING, catalog_name STRING, schema_name STRING,
  classification_code STRING, lifecycle_status STRING, home_workspace_id STRING);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_schema (
  securable_id STRING NOT NULL, securable_type STRING, full_name STRING, catalog_name STRING,
  data_product_id STRING, classification_code STRING)
COMMENT 'Unity Catalog securables governed by the app (catalogs, schemas, tables)';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_access_group (
  access_group_id STRING NOT NULL, group_name STRING, purpose STRING, privilege_level STRING,
  classification_code STRING, status STRING, requestable BOOLEAN, data_product_id STRING, project_id STRING,
  ai_use_case_code STRING, owner_email STRING);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.dim_asset (
  asset_id STRING NOT NULL, asset_type STRING, name STRING, external_id STRING, workspace_id STRING,
  data_product_id STRING, ai_use_case_code STRING, classification_code STRING, is_active BOOLEAN);

-- ---------------- Bridges (many-to-many) ----------------
CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.bridge_project_data_product (
  project_id STRING NOT NULL, data_product_id STRING NOT NULL, linked_by STRING, linked_at TIMESTAMP);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.bridge_group_entitlement (
  access_group_id STRING NOT NULL, securable_id STRING, asset_id STRING, privilege STRING,
  desired_state STRING, apply_status STRING);

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.bridge_schema_logical_group (
  securable_id STRING NOT NULL, logical_group_code STRING NOT NULL);

-- ---------------- Facts ----------------
CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.fact_access_request (
  request_id STRING NOT NULL, request_ref STRING, request_type STRING,
  requester_user_sk BIGINT, beneficiary_user_sk BIGINT, access_group_id STRING, project_id STRING,
  data_product_id STRING, classification_code STRING, status_code STRING,
  submitted_date_key INT, decided_date_key INT, fulfilled_date_key INT,
  submitted_at TIMESTAMP, decided_at TIMESTAMP, fulfilled_at TIMESTAMP,
  hours_to_decision DOUBLE, hours_to_fulfil DOUBLE, stage_count INT, any_sla_breach BOOLEAN,
  requested_duration_days INT)
COMMENT 'Grain: one row per access request';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.fact_approval_event (
  task_id STRING NOT NULL, request_id STRING, stage_no INT, approver_role STRING, status_code STRING,
  is_post_hoc BOOLEAN, decided_by_user_sk BIGINT, on_behalf_of_user_sk BIGINT, created_at TIMESTAMP,
  due_at TIMESTAMP, decided_at TIMESTAMP, decided_date_key INT, hours_to_decide DOUBLE, sla_breached BOOLEAN,
  escalated BOOLEAN)
COMMENT 'Grain: one row per approval task (stage) of a request';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.fact_grant_lifecycle (
  grant_id STRING NOT NULL, request_id STRING, user_sk BIGINT, access_group_id STRING, data_product_id STRING,
  classification_code STRING, status_code STRING, is_break_glass BOOLEAN,
  requested_at TIMESTAMP, approved_at TIMESTAMP, provisioned_at TIMESTAMP, valid_to TIMESTAMP,
  last_recertified_at TIMESTAMP, revoke_requested_at TIMESTAMP, revoked_at TIMESTAMP, revoke_reason STRING,
  provisioned_date_key INT, revoked_date_key INT, hours_approval_to_provision DOUBLE,
  hours_revoke_to_removed DOUBLE, days_active INT)
COMMENT 'Accumulating snapshot. Grain: one row per grant, milestones filled in as they happen';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.fact_access_snapshot (
  snapshot_date DATE NOT NULL, snapshot_date_key INT, grant_id STRING NOT NULL, user_sk BIGINT,
  access_group_id STRING, data_product_id STRING, classification_code STRING, is_break_glass BOOLEAN,
  days_to_expiry INT, days_since_recert INT, recert_overdue BOOLEAN)
PARTITIONED BY (snapshot_date)
COMMENT 'Periodic snapshot. Grain: one row per live grant per day';

-- ---------------- Long-term audit retention ----------------
CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.audit_event_archive (
  chain_seq BIGINT NOT NULL, occurred_at TIMESTAMP, actor STRING, actor_type STRING, action STRING,
  object_kind STRING, object_id STRING, correlation_id STRING, before_state STRING, after_state STRING,
  reason STRING, prev_hash STRING, event_hash STRING)
TBLPROPERTIES ('delta.appendOnly' = 'true', 'delta.deletedFileRetentionDuration' = 'interval 30 days')
COMMENT 'Append-only copy of the Lakebase audit trail, retained for the regulatory period';

CREATE TABLE IF NOT EXISTS ${catalog}.${schema}.drift_finding_history (
  finding_id STRING, run_id STRING, finding_type STRING, severity STRING, securable STRING, principal STRING,
  privilege STRING, status STRING, first_detected_at TIMESTAMP, last_detected_at TIMESTAMP,
  resolved_at TIMESTAMP, resolved_by STRING, resolution_note STRING);
