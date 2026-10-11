-- =============================================================================
-- Migration 005: approval by the data product's two approvers; no classification.
--
--   * Every request is approved in one stage by the data product's owner / approver
--     or its approver 2 (either may decide). The steward role goes: its column
--     becomes approver2_email (Delta column mapping), so existing values carry over.
--   * Classification is no longer recorded. The columns become nullable and stay
--     for history; the classification and approval_rule tables are kept, unused,
--     for a possible future requirement.
--   * Pending approval tasks are moved to the single APPROVER stage.
--
-- Safe to re-run up to the last statement: the column rename is last, so a re-run
-- after a failure earlier in the file repeats only idempotent statements.
-- =============================================================================

-- ---- classification no longer recorded ---------------------------------------
ALTER TABLE data_product ALTER COLUMN classification_code DROP NOT NULL;
ALTER TABLE access_request ALTER COLUMN classification_code DROP NOT NULL;
-- Delta treats a NULL check result as a violation, so NULL must pass explicitly
ALTER TABLE data_product DROP CONSTRAINT IF EXISTS data_product_class_ck;
ALTER TABLE data_product ADD CONSTRAINT data_product_class_ck
  CHECK (classification_code IS NULL OR classification_code IN ('GENERAL','CONFIDENTIAL','RESTRICTED'));

-- ---- one approval stage -------------------------------------------------------
-- Requests waiting at any stage are decided by the data product's approvers from now on
UPDATE approval_task SET approver_role = 'APPROVER' WHERE status = 'PENDING' AND NOT is_post_hoc;

-- ---- steward becomes approver 2 -----------------------------------------------
ALTER TABLE data_product SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name');
ALTER TABLE data_product RENAME COLUMN steward_email TO approver2_email;
