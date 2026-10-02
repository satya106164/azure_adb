-- =============================================================================
-- Lakebase roles and grants for the two-app design.
--
--   Jobs (provisioning SP)  owns the schema; runs migrations, provisioning, lifecycle
--   Request app SP          requests, approvals, reviews, delegations, owner project links
--   Admin app SP            reference data, entitlements, groups, retries, drift, audit
--   Auditors group          read-only (optional, for direct SQL access)
--
-- PART 1 is run MANUALLY once per environment by the instance owner.
-- PART 2 (between the GRANTS markers) is re-applied automatically by the
-- cdp_security-migrate job after every migration, with the role names passed as
-- job parameters, so new tables never miss their grants. It starts by revoking
-- everything, so removing a grant here removes it in the database.
-- =============================================================================

-- ---------------- PART 1: identities (manual, once) ----------------
CREATE EXTENSION IF NOT EXISTS databricks_auth;
SELECT databricks_create_role('<provisioning-sp-application-id>', 'SERVICE_PRINCIPAL');
-- The apps' roles are usually created by their database resource; create them if not:
SELECT databricks_create_role('<request-app-sp-application-id>', 'SERVICE_PRINCIPAL');
SELECT databricks_create_role('<admin-app-sp-application-id>', 'SERVICE_PRINCIPAL');
SELECT databricks_create_role('<auditors-group-name>', 'GROUP');
GRANT CREATE ON DATABASE databricks_postgres TO "<provisioning-sp-application-id>";

-- ---------------- PART 2: grants (applied by cdp_security-migrate) ----------------
-- BEGIN GRANTS
SET search_path = cdp_security, public;

REVOKE ALL ON ALL TABLES IN SCHEMA cdp_security FROM {{REQUEST_APP}}, {{ADMIN_APP}};
REVOKE ALL ON ALL SEQUENCES IN SCHEMA cdp_security FROM {{REQUEST_APP}}, {{ADMIN_APP}};
GRANT USAGE ON SCHEMA cdp_security TO {{REQUEST_APP}}, {{ADMIN_APP}};
GRANT USAGE ON ALL SEQUENCES IN SCHEMA cdp_security TO {{REQUEST_APP}}, {{ADMIN_APP}};

-- Both apps: read reference and workflow data, register users, write audit and notifications
GRANT SELECT ON classification, approval_rule, logical_group, app_role_member, workspace, data_product,
                project, project_data_product, uc_securable, securable_logical_group, ai_use_case, asset,
                access_group, ownership, delegation, access_request, approval_task, access_grant,
                recert_campaign, recert_item, v_group, v_request, v_grant, v_task
  TO {{REQUEST_APP}}, {{ADMIN_APP}};
GRANT SELECT, INSERT, UPDATE ON app_user TO {{REQUEST_APP}}, {{ADMIN_APP}};
GRANT INSERT ON audit_event TO {{REQUEST_APP}}, {{ADMIN_APP}};
GRANT SELECT, INSERT ON notification_outbox TO {{REQUEST_APP}}, {{ADMIN_APP}};

-- Request app: the workflow, and project links made by data owners
GRANT INSERT, UPDATE ON access_request, approval_task, access_grant TO {{REQUEST_APP}};
GRANT INSERT ON delegation, project_data_product TO {{REQUEST_APP}};
GRANT UPDATE ON recert_item TO {{REQUEST_APP}};

-- Admin app: catalog and ownership, group lifecycle, operations, drift, audit
GRANT INSERT, UPDATE ON workspace, data_product, project, project_data_product, uc_securable,
                        securable_logical_group, ai_use_case, asset, access_group, ownership
  TO {{ADMIN_APP}};
GRANT SELECT, INSERT, UPDATE ON group_entitlement TO {{ADMIN_APP}};
GRANT INSERT, DELETE ON app_role_member TO {{ADMIN_APP}};
GRANT UPDATE ON access_request, approval_task, access_grant TO {{ADMIN_APP}};   -- retire group, retry
GRANT SELECT, UPDATE ON provisioning_action, drift_finding TO {{ADMIN_APP}};
GRANT SELECT ON drift_run, audit_event TO {{ADMIN_APP}};
GRANT EXECUTE ON FUNCTION verify_audit_chain() TO {{ADMIN_APP}};

-- Auditors (optional): read-only on everything
GRANT USAGE ON SCHEMA cdp_security TO {{AUDITOR}};
GRANT SELECT ON ALL TABLES IN SCHEMA cdp_security TO {{AUDITOR}};
GRANT EXECUTE ON FUNCTION verify_audit_chain() TO {{AUDITOR}};
-- END GRANTS

-- Not granted to either app, deliberately:
--   approval_rule / classification changes   (governance migrations only)
--   UPDATE / DELETE on audit_event           (append-only; triggers also block it)
--   provisioning_action, group_entitlement, drift_* for the request app
--   access_request / approval_task INSERT for the admin app (admins cannot raise or approve through it)
