-- Business views over the star schema (used by the AI/BI dashboard or Power BI)
CREATE OR REPLACE VIEW ${catalog}.${schema}.v_open_requests AS
SELECT f.request_ref, f.request_type, f.status_code, f.classification_code, g.group_name,
       u.email AS beneficiary_email, f.submitted_at, f.any_sla_breach
FROM ${catalog}.${schema}.fact_access_request f
JOIN ${catalog}.${schema}.dim_access_group g ON g.access_group_id = f.access_group_id
LEFT JOIN ${catalog}.${schema}.dim_user u ON u.user_sk = f.beneficiary_user_sk
WHERE f.status_code IN ('PENDING_APPROVAL', 'APPROVED', 'PROVISIONING', 'PROVISIONING_FAILED');

CREATE OR REPLACE VIEW ${catalog}.${schema}.v_sla_performance AS
SELECT date_trunc('MONTH', decided_at) AS month, approver_role, count(*) AS decisions,
       avg(hours_to_decide) AS avg_hours, sum(CASE WHEN sla_breached THEN 1 ELSE 0 END) AS breaches
FROM ${catalog}.${schema}.fact_approval_event
WHERE decided_at IS NOT NULL
GROUP BY 1, 2;

CREATE OR REPLACE VIEW ${catalog}.${schema}.v_access_by_classification AS
SELECT s.snapshot_date, s.classification_code, d.code AS data_product, count(*) AS live_grants,
       sum(CASE WHEN s.recert_overdue THEN 1 ELSE 0 END) AS overdue_recert,
       sum(CASE WHEN s.is_break_glass THEN 1 ELSE 0 END) AS break_glass
FROM ${catalog}.${schema}.fact_access_snapshot s
LEFT JOIN ${catalog}.${schema}.dim_data_product d ON d.data_product_id = s.data_product_id
GROUP BY 1, 2, 3;

CREATE OR REPLACE VIEW ${catalog}.${schema}.v_who_has_access_now AS
SELECT u.email, u.department, g.group_name, g.classification_code, d.code AS data_product,
       s.days_to_expiry, s.days_since_recert, s.is_break_glass
FROM ${catalog}.${schema}.fact_access_snapshot s
JOIN ${catalog}.${schema}.dim_user u ON u.user_sk = s.user_sk
JOIN ${catalog}.${schema}.dim_access_group g ON g.access_group_id = s.access_group_id
LEFT JOIN ${catalog}.${schema}.dim_data_product d ON d.data_product_id = s.data_product_id
WHERE s.snapshot_date = (SELECT max(snapshot_date) FROM ${catalog}.${schema}.fact_access_snapshot);
