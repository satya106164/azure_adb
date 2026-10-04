-- =============================================================================
-- Migration 000: remove the reporting tables of the earlier Lakebase design.
-- They lived in this same schema and some of their names are reused by the
-- Delta design with different columns. Safe where they never existed.
-- =============================================================================
DROP VIEW IF EXISTS v_open_requests;
DROP VIEW IF EXISTS v_sla_performance;
DROP VIEW IF EXISTS v_access_by_classification;
DROP VIEW IF EXISTS v_who_has_access_now;
DROP TABLE IF EXISTS dim_date;
DROP TABLE IF EXISTS dim_user;
DROP TABLE IF EXISTS dim_classification;
DROP TABLE IF EXISTS dim_status;
DROP TABLE IF EXISTS dim_workspace;
DROP TABLE IF EXISTS dim_project;
DROP TABLE IF EXISTS dim_data_product;
DROP TABLE IF EXISTS dim_schema;
DROP TABLE IF EXISTS dim_access_group;
DROP TABLE IF EXISTS dim_asset;
DROP TABLE IF EXISTS bridge_project_data_product;
DROP TABLE IF EXISTS bridge_group_entitlement;
DROP TABLE IF EXISTS bridge_schema_logical_group;
DROP TABLE IF EXISTS fact_access_request;
DROP TABLE IF EXISTS fact_approval_event;
DROP TABLE IF EXISTS fact_grant_lifecycle;
DROP TABLE IF EXISTS fact_access_snapshot;
DROP TABLE IF EXISTS audit_event_archive;
DROP TABLE IF EXISTS drift_finding_history;
