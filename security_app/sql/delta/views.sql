-- =============================================================================
-- Read views used by the apps and jobs. Re-applied by jobs/migrate.py on every
-- run (CREATE OR REPLACE), so edit this file freely.
-- =============================================================================
CREATE OR REPLACE VIEW v_group AS
SELECT g.group_code, g.group_seq, g.group_name, g.data_product_code, g.status, g.requestable,
       g.description, g.created_by, g.created_at, g.updated_by, g.updated_at,
       d.name AS data_product_name, d.data_product_type, d.project_name, d.catalog_name, d.schema_name,
       d.classification_code, d.owner_email, d.steward_email, d.home_workspace_code,
       d.lifecycle_status AS data_product_status, w.name AS workspace_name
FROM access_group g
JOIN data_product d ON d.data_product_code = g.data_product_code
LEFT JOIN workspace w ON w.workspace_code = d.home_workspace_code;

CREATE OR REPLACE VIEW v_request AS
SELECT r.request_id, r.request_ref, r.request_type, r.status, r.classification_code, r.current_stage_no,
       r.justification, r.incident_ref, r.requested_duration_days, r.extends_grant_id,
       r.submitted_at, r.decided_at, r.fulfilled_at, r.created_at,
       r.requester_email, r.beneficiary_email,
       g.group_code, g.group_name, g.data_product_code, g.data_product_name, g.data_product_type,
       s.total_stages
FROM access_request r
JOIN v_group g ON g.group_code = r.group_code
LEFT JOIN (SELECT classification_code, max(stage_no) AS total_stages FROM approval_rule
           GROUP BY classification_code) s ON s.classification_code = r.classification_code;

CREATE OR REPLACE VIEW v_grant AS
SELECT gr.grant_id, gr.request_id, gr.status, gr.is_break_glass, gr.valid_from, gr.valid_to,
       gr.provisioned_at, gr.provisioned_by, gr.fulfilment_note, gr.revoke_requested_at, gr.revoked_at,
       gr.revoke_reason, gr.revoked_by, gr.removed_by, gr.removal_note, gr.last_recertified_at,
       gr.expiry_notice_level, gr.created_at, gr.user_email, u.display_name,
       g.group_code, g.group_name, g.classification_code, g.data_product_code, g.data_product_name,
       g.data_product_type, g.owner_email, g.steward_email, r.request_ref, r.request_type, r.decided_at,
       r.incident_ref
FROM access_grant gr
JOIN v_group g ON g.group_code = gr.group_code
JOIN access_request r ON r.request_id = gr.request_id
LEFT JOIN app_user u ON u.email = gr.user_email;

CREATE OR REPLACE VIEW v_task AS
SELECT t.task_id, t.stage_no, t.approver_role, t.status AS task_status, t.is_post_hoc, t.due_at,
       t.escalated_at, t.decided_at AS task_decided_at, t.decision_comment,
       t.decided_by_email, t.on_behalf_of_email,
       r.*
FROM approval_task t
JOIN v_request r ON r.request_id = t.request_id;
