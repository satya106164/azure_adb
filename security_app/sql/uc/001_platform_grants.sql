-- =============================================================================
-- Unity Catalog grants for the app's own identities. Run once per environment
-- by a metastore / catalog admin. Replace <placeholders>.
-- (In dev the deploying user owns dev_gold, so the migrate job does the app parts itself.)
--
-- The app never changes access to data products: admins add / remove group
-- members and grant group permissions by hand. So no identity here gets MANAGE.
-- =============================================================================

-- Jobs SP: creates and owns the app's schema inside an EXISTING catalog (the app never creates catalogs).
-- <app_catalog>.<app_schema> is the bundle's catalog / schema (dev: dev_gold.cdp_security).
GRANT USE CATALOG, CREATE SCHEMA ON CATALOG <app_catalog> TO `<jobs-sp-application-id>`;

-- Both apps' service principals: reach the schema. The migrate job then grants them
-- per-table SELECT / MODIFY (app/cdp_security/appgrants.py).
GRANT USE CATALOG ON CATALOG <app_catalog> TO `<request-app-sp-application-id>`;
GRANT USE CATALOG ON CATALOG <app_catalog> TO `<admin-app-sp-application-id>`;

-- Readers of the whole schema, reporting model included (contains user emails - keep it narrow).
-- The migrate job grants USE SCHEMA, SELECT on the schema to these groups.
GRANT USE CATALOG ON CATALOG <app_catalog> TO `grp-dbx-<env>-app-cdp_security-auditor`;
GRANT USE CATALOG ON CATALOG <app_catalog> TO `grp-dbx-<env>-app-cdp_security-security`;

-- SQL warehouse: the jobs SP needs CAN_USE on the warehouse (Warehouse > Permissions).
-- The apps get CAN_USE through their sql_warehouse resource in resources/app.yml.
