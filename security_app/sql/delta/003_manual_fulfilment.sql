-- =============================================================================
-- Migration 003: the app records and approves access; people change it.
--
--   * No automated provisioning: an admin adds / removes group members by hand
--     and marks each grant done in the Catalog admin app (who, when, note).
--     The provisioning queue, desired-grant table and drift tables go.
--   * Groups are registered as existing groups: ACTIVE from registration.
--   * Data products get a type and an optional project; catalog and schema are
--     required only for the SCHEMA type.
--   * Workspace types: PROJECT and DBANALYTICS replaced by DATABRICKS_DASHBOARDS.
--
-- Safe to re-run: constraints are dropped IF EXISTS before being re-added, and
-- columns that already exist are skipped by the migration runner.
-- =============================================================================

-- ---- workspace ----------------------------------------------------------------
ALTER TABLE workspace DROP CONSTRAINT IF EXISTS workspace_type_ck;
ALTER TABLE workspace ADD CONSTRAINT workspace_type_ck
  CHECK (workspace_type IN ('INGESTION','ANALYTICS_PBI','MLOPS','DATABRICKS_DASHBOARDS'));

-- ---- data_product -----------------------------------------------------------
ALTER TABLE data_product ADD COLUMNS (
  data_product_type  STRING COMMENT 'DATABRICKS_DASHBOARD | POWER_BI | GENIE_AGENT | CUSTOM_AGENT | DATABRICKS_APP | SCHEMA',
  project_name       STRING COMMENT 'free text, optional'
);
UPDATE data_product SET data_product_type = 'SCHEMA' WHERE data_product_type IS NULL;
ALTER TABLE data_product ALTER COLUMN data_product_type SET NOT NULL;
ALTER TABLE data_product ALTER COLUMN catalog_name DROP NOT NULL;
ALTER TABLE data_product ALTER COLUMN schema_name DROP NOT NULL;
-- Delta treats a NULL check result as a violation, so empty names must pass explicitly
ALTER TABLE data_product DROP CONSTRAINT IF EXISTS data_product_names_ck;
ALTER TABLE data_product ADD CONSTRAINT data_product_names_ck
  CHECK (coalesce(catalog_name = lower(catalog_name), true) AND coalesce(schema_name = lower(schema_name), true));
ALTER TABLE data_product DROP CONSTRAINT IF EXISTS data_product_type_ck;
ALTER TABLE data_product ADD CONSTRAINT data_product_type_ck
  CHECK (data_product_type IN ('DATABRICKS_DASHBOARD','POWER_BI','GENIE_AGENT','CUSTOM_AGENT','DATABRICKS_APP','SCHEMA'));
ALTER TABLE data_product DROP CONSTRAINT IF EXISTS data_product_schema_ck;
ALTER TABLE data_product ADD CONSTRAINT data_product_schema_ck
  CHECK ((data_product_type <> 'SCHEMA' OR (catalog_name IS NOT NULL AND schema_name IS NOT NULL))
         AND (schema_name IS NULL OR catalog_name IS NOT NULL));

-- ---- access_group: registered groups already exist, so they are ACTIVE ---------------
ALTER TABLE access_group DROP CONSTRAINT IF EXISTS access_group_active_ck;
ALTER TABLE access_group DROP CONSTRAINT IF EXISTS access_group_status_ck;
UPDATE access_group SET status = 'ACTIVE', updated_at = current_timestamp() WHERE status = 'PLANNED';
ALTER TABLE access_group ADD CONSTRAINT access_group_status_ck CHECK (status IN ('ACTIVE','RETIRING','RETIRED'));

-- ---- access_request: no provisioning states -------------------------------------------
ALTER TABLE access_request DROP CONSTRAINT IF EXISTS access_request_status_ck;
ALTER TABLE access_request ADD CONSTRAINT access_request_status_ck
  CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','FULFILLED','REJECTED','CANCELLED'));

-- ---- access_grant: who did the manual change ------------------------------------------
ALTER TABLE access_grant ADD COLUMNS (
  provisioned_by   STRING COMMENT 'admin who added the member and marked it granted',
  fulfilment_note  STRING,
  removed_by       STRING COMMENT 'admin who removed the member and marked it removed',
  removal_note     STRING
);
ALTER TABLE access_grant DROP CONSTRAINT IF EXISTS access_grant_status_ck;
ALTER TABLE access_grant ADD CONSTRAINT access_grant_status_ck
  CHECK (status IN ('PENDING_PROVISION','ACTIVE','PENDING_REVOKE','REVOKED','EXPIRED'));

-- ---- automation tables no longer used ----------------------------------------------------
DROP TABLE IF EXISTS provisioning_action;
DROP TABLE IF EXISTS group_entitlement;
DROP TABLE IF EXISTS drift_finding;
DROP TABLE IF EXISTS drift_run;
