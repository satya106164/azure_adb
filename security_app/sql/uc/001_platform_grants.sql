-- =============================================================================
-- Unity Catalog grants for the app's own identities. Run once per environment
-- by a metastore / catalog admin. Replace <placeholders>.
-- =============================================================================

-- Provisioning SP: manage grants on every governed catalog (never ownership)
GRANT USE CATALOG, MANAGE ON CATALOG dbanalytics TO `<provisioning-sp-application-id>`;
-- repeat for analytics / mlops / project catalogs that the app governs

-- Provisioning SP: read actual state for drift detection
GRANT USE CATALOG ON CATALOG system TO `<provisioning-sp-application-id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.access TO `<provisioning-sp-application-id>`;
GRANT USE SCHEMA, SELECT ON SCHEMA system.information_schema TO `<provisioning-sp-application-id>`;

-- Provisioning SP: builds the reporting schema inside an EXISTING catalog (the app never creates catalogs).
-- <reporting_catalog>.<reporting_schema> is the bundle's catalog / schema (dev: dev_gold.cdp_security).
GRANT USE CATALOG, CREATE SCHEMA ON CATALOG <reporting_catalog> TO `<provisioning-sp-application-id>`;

-- Readers of the reporting model (contains user emails - keep it narrow)
GRANT USE CATALOG ON CATALOG <reporting_catalog> TO `grp-dbx-<env>-app-cdp_security-auditor`;
GRANT USE SCHEMA, SELECT ON SCHEMA <reporting_catalog>.<reporting_schema> TO `grp-dbx-<env>-app-cdp_security-auditor`;
GRANT USE CATALOG ON CATALOG <reporting_catalog> TO `grp-dbx-<env>-app-cdp_security-security`;
GRANT USE SCHEMA, SELECT ON SCHEMA <reporting_catalog>.<reporting_schema> TO `grp-dbx-<env>-app-cdp_security-security`;

-- The App SP needs NO Unity Catalog data privileges: it only talks to Lakebase.
