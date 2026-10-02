-- =============================================================================
-- Migration 002: reference data. Approval routes and limits are governance
-- decisions: change them with a new migration that went through design review.
-- =============================================================================
SET search_path = cdp_security, public;

INSERT INTO classification (classification_code, rank, max_grant_days, recert_interval_days, requires_project, description) VALUES
  ('GENERAL',      1, 365, 365, false, 'Default. Internal data with no personal or commercially sensitive content.'),
  ('CONFIDENTIAL', 2, 180, 180, true,  'Personal data or commercially sensitive data. Explicitly tagged via CI/CD.'),
  ('RESTRICTED',   3,  90,  90, true,  'Special category / high-risk member data, regulatory submissions. Explicitly tagged via CI/CD.');

-- Sequential approval stages per classification. SLA is in business hours
-- (24 business hours = 1 working day, UK calendar).
INSERT INTO approval_rule (classification_code, stage_no, approver_role, sla_hours) VALUES
  ('GENERAL',      1, 'DATA_OWNER', 72),
  ('CONFIDENTIAL', 1, 'DATA_OWNER', 72),
  ('CONFIDENTIAL', 2, 'STEWARD',    72),
  ('RESTRICTED',   1, 'DATA_OWNER', 48),
  ('RESTRICTED',   2, 'STEWARD',    48),
  ('RESTRICTED',   3, 'SECURITY',   48);

INSERT INTO logical_group (logical_group_code, name, description) VALUES
  ('CUSTOMER',   'Customer',   'Member and customer data'),
  ('FINANCE',    'Finance',    'Finance, contributions, payments'),
  ('RISK',       'Risk',       'Risk, actuarial and capital'),
  ('REGULATORY', 'Regulatory', 'Regulatory reporting and submissions');
