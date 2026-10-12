-- =============================================================================
-- Migration 006: break-glass (emergency access) is removed completely.
--
--   * Every break-glass request is deleted, with its approval tasks, grants,
--     access review items and notifications.
--   * Its columns go: access_request.incident_ref, access_grant.is_break_glass,
--     approval_task.is_post_hoc (Delta column mapping is turned on to drop them).
--   * Request type is GRANT or EXTEND only.
--   * The reporting facts that carried the break-glass flags are dropped here and
--     rebuilt without them by sql/reporting/reporting.sql and the daily job.
--
-- audit_event is not touched: it is hash-chained, so deleting past events would
-- make the chain fail verification. Earlier BREAK_GLASS_* events stay in it.
--
-- Safe to re-run: rows are found by request_type (which stays), constraints are
-- dropped IF EXISTS before being re-added, and columns are dropped IF EXISTS.
-- =============================================================================

-- ---- delete break-glass rows (dependants first) ---------------------------------------
DELETE FROM recert_item WHERE grant_id IN (
  SELECT g.grant_id FROM access_grant g JOIN access_request r ON r.request_id = g.request_id
  WHERE r.request_type = 'BREAK_GLASS');
DELETE FROM notification_outbox
WHERE template LIKE 'BREAK_GLASS%'
   OR request_id IN (SELECT request_id FROM access_request WHERE request_type = 'BREAK_GLASS');
DELETE FROM approval_task
WHERE request_id IN (SELECT request_id FROM access_request WHERE request_type = 'BREAK_GLASS');
DELETE FROM access_grant
WHERE request_id IN (SELECT request_id FROM access_request WHERE request_type = 'BREAK_GLASS');
DELETE FROM access_request WHERE request_type = 'BREAK_GLASS';

-- ---- constraints -------------------------------------------------------------------------
ALTER TABLE access_request DROP CONSTRAINT IF EXISTS access_request_bg_ck;
ALTER TABLE access_request DROP CONSTRAINT IF EXISTS access_request_type_ck;
ALTER TABLE access_request ADD CONSTRAINT access_request_type_ck CHECK (request_type IN ('GRANT','EXTEND'));

-- ---- reporting facts with break-glass columns (rebuilt by reporting.sql) ---------------
DROP TABLE IF EXISTS fact_approval_event;
DROP TABLE IF EXISTS fact_grant_lifecycle;
DROP TABLE IF EXISTS fact_access_snapshot;

-- ---- drop the columns ------------------------------------------------------------------
ALTER TABLE access_request SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name');
ALTER TABLE access_request DROP COLUMN IF EXISTS incident_ref;
ALTER TABLE access_grant SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name');
ALTER TABLE access_grant DROP COLUMN IF EXISTS is_break_glass;
ALTER TABLE approval_task SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name');
ALTER TABLE approval_task DROP COLUMN IF EXISTS is_post_hoc;
