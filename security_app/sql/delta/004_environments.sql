-- =============================================================================
-- Migration 004: four environments, and an environment on every data product.
--
--   * Workspaces and data products use dev | tst | pre | prd
--     (shown as Dev / Test / Pre-prod / Prod).
--   * Existing data products take their home workspace's environment, else dev.
--   * A data product name is unique within its environment, so the same product
--     can be registered once per environment.
--
-- Safe to re-run: constraints are dropped IF EXISTS before being re-added, and
-- columns that already exist are skipped by the migration runner.
-- =============================================================================

-- ---- workspace ----------------------------------------------------------------
ALTER TABLE workspace DROP CONSTRAINT IF EXISTS workspace_env_ck;
ALTER TABLE workspace ADD CONSTRAINT workspace_env_ck CHECK (environment IN ('dev','tst','pre','prd'));

-- ---- data_product -----------------------------------------------------------
ALTER TABLE data_product ADD COLUMNS (
  environment  STRING COMMENT 'dev | tst | pre | prd'
);
MERGE INTO data_product d
USING workspace w ON w.workspace_code = d.home_workspace_code
WHEN MATCHED AND d.environment IS NULL THEN UPDATE SET d.environment = w.environment;
UPDATE data_product SET environment = 'dev' WHERE environment IS NULL;
ALTER TABLE data_product ALTER COLUMN environment SET NOT NULL;
ALTER TABLE data_product DROP CONSTRAINT IF EXISTS data_product_env_ck;
ALTER TABLE data_product ADD CONSTRAINT data_product_env_ck CHECK (environment IN ('dev','tst','pre','prd'));
