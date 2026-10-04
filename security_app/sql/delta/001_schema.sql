-- =============================================================================
-- Security Access Workflow - operational store (Delta tables, Unity Catalog)
-- Migration 001: core schema. Applied once by jobs/migrate.py (tracked in
-- schema_migration). Never edit after it has been applied; add a new numbered
-- migration instead.
--
-- Delta has no multi-table transactions and no enforced keys. The app keeps
-- the rules itself:
--   * every check-and-write is ONE statement (INSERT ... SELECT ... WHERE NOT
--     EXISTS, UPDATE ... WHERE status = <expected>) and the row count is checked;
--   * every table uses Serializable isolation, so two statements racing on the
--     same table conflict and the loser is retried (db.py);
--   * generated identifiers (WS-0001, DP-0001, GRP-0001, SAR-0000001) are
--     max + 1 inside the inserting statement, so they have no gaps.
-- Key columns are marked "key" in comments; CHECK constraints are enforced.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Reference data (changed only by migration: design governance)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS classification (
  classification_code   STRING NOT NULL COMMENT 'key',
  rank                  INT NOT NULL,
  max_grant_days        INT NOT NULL,
  recert_interval_days  INT NOT NULL,
  description           STRING
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable');

CREATE TABLE IF NOT EXISTS approval_rule (
  classification_code  STRING NOT NULL COMMENT 'key part 1',
  stage_no             INT NOT NULL COMMENT 'key part 2',
  approver_role        STRING NOT NULL,
  sla_hours            INT NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable');
ALTER TABLE approval_rule ADD CONSTRAINT approval_rule_role CHECK (approver_role IN ('DATA_OWNER','STEWARD','SECURITY'));

-- ---------------------------------------------------------------------------
-- People (keyed by lower-case work email)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_user (
  email            STRING NOT NULL COMMENT 'key, lower case',
  entra_object_id  STRING,
  display_name     STRING,
  department       STRING,
  is_active        BOOLEAN NOT NULL,
  last_seen_at     TIMESTAMP,
  deactivated_at   TIMESTAMP,
  created_at       TIMESTAMP NOT NULL,
  updated_at       TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE app_user ADD CONSTRAINT app_user_email CHECK (email = lower(email) AND email LIKE '%@%');

-- ---------------------------------------------------------------------------
-- Catalog: workspaces, data products, access groups (registered in the Catalog admin app)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace (
  workspace_code           STRING NOT NULL COMMENT 'key, generated WS-nnnn',
  workspace_seq            BIGINT NOT NULL,
  name                     STRING NOT NULL,
  workspace_type           STRING NOT NULL,
  host_url                 STRING NOT NULL,
  databricks_workspace_id  BIGINT COMMENT 'derived from the URL',
  environment              STRING NOT NULL,
  is_active                BOOLEAN NOT NULL,
  created_by               STRING NOT NULL,
  created_at               TIMESTAMP NOT NULL,
  updated_by               STRING,
  updated_at               TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE workspace ADD CONSTRAINT workspace_type_ck
  CHECK (workspace_type IN ('INGESTION','PROJECT','DBANALYTICS','ANALYTICS_PBI','MLOPS'));
ALTER TABLE workspace ADD CONSTRAINT workspace_env_ck CHECK (environment IN ('dev','tst','prd'));
ALTER TABLE workspace ADD CONSTRAINT workspace_url_ck CHECK (host_url LIKE 'https://%');

CREATE TABLE IF NOT EXISTS data_product (
  data_product_code    STRING NOT NULL COMMENT 'key, generated DP-nnnn',
  data_product_seq     BIGINT NOT NULL,
  name                 STRING NOT NULL,
  description          STRING,
  catalog_name         STRING NOT NULL,
  schema_name          STRING NOT NULL COMMENT 'catalog_name + schema_name is unique',
  classification_code  STRING NOT NULL,
  home_workspace_code  STRING,
  owner_email          STRING NOT NULL,
  steward_email        STRING NOT NULL,
  lifecycle_status     STRING NOT NULL,
  created_by           STRING NOT NULL,
  created_at           TIMESTAMP NOT NULL,
  updated_by           STRING,
  updated_at           TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE data_product ADD CONSTRAINT data_product_names_ck
  CHECK (catalog_name = lower(catalog_name) AND schema_name = lower(schema_name));
ALTER TABLE data_product ADD CONSTRAINT data_product_class_ck
  CHECK (classification_code IN ('GENERAL','CONFIDENTIAL','RESTRICTED'));
ALTER TABLE data_product ADD CONSTRAINT data_product_status_ck CHECK (lifecycle_status IN ('ACTIVE','RETIRED'));

-- Entra ID security groups. Each gives READ on exactly one data product.
CREATE TABLE IF NOT EXISTS access_group (
  group_code         STRING NOT NULL COMMENT 'key, generated GRP-nnnn',
  group_seq          BIGINT NOT NULL,
  group_name         STRING NOT NULL COMMENT 'unique, grp-dbx-<env>-dp-<name>-read',
  data_product_code  STRING NOT NULL,
  entra_object_id    STRING COMMENT 'recorded automatically (Graph), never typed',
  status             STRING NOT NULL,
  requestable        BOOLEAN NOT NULL,
  description        STRING,
  created_by         STRING NOT NULL,
  created_at         TIMESTAMP NOT NULL,
  updated_by         STRING,
  updated_at         TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE access_group ADD CONSTRAINT access_group_status_ck
  CHECK (status IN ('PLANNED','ACTIVE','RETIRING','RETIRED'));
ALTER TABLE access_group ADD CONSTRAINT access_group_active_ck
  CHECK (status <> 'ACTIVE' OR entra_object_id IS NOT NULL);

-- Desired state: the Unity Catalog grants each group holds (generated from the read template)
CREATE TABLE IF NOT EXISTS group_entitlement (
  entitlement_id   STRING NOT NULL COMMENT 'key',
  group_code       STRING NOT NULL,
  securable_type   STRING NOT NULL,
  full_name        STRING NOT NULL,
  privilege        STRING NOT NULL,
  desired_state    STRING NOT NULL,
  apply_status     STRING NOT NULL,
  last_applied_at  TIMESTAMP,
  last_error       STRING,
  created_by       STRING NOT NULL,
  created_at       TIMESTAMP NOT NULL,
  updated_at       TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE group_entitlement ADD CONSTRAINT group_entitlement_type_ck CHECK (securable_type IN ('CATALOG','SCHEMA'));
ALTER TABLE group_entitlement ADD CONSTRAINT group_entitlement_state_ck CHECK (desired_state IN ('PRESENT','ABSENT'));
ALTER TABLE group_entitlement ADD CONSTRAINT group_entitlement_apply_ck
  CHECK (apply_status IN ('PENDING','APPLIED','REMOVED','FAILED'));

CREATE TABLE IF NOT EXISTS delegation (
  delegation_id    STRING NOT NULL COMMENT 'key',
  delegator_email  STRING NOT NULL,
  delegate_email   STRING NOT NULL,
  valid_from       TIMESTAMP NOT NULL,
  valid_to         TIMESTAMP NOT NULL,
  reason           STRING NOT NULL,
  created_by       STRING NOT NULL,
  created_at       TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable');
ALTER TABLE delegation ADD CONSTRAINT delegation_ck
  CHECK (delegator_email <> delegate_email AND valid_to > valid_from);

-- ---------------------------------------------------------------------------
-- Workflow
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS access_request (
  request_id               STRING NOT NULL COMMENT 'key',
  request_seq              BIGINT NOT NULL,
  request_ref              STRING NOT NULL COMMENT 'generated SAR-nnnnnnn',
  request_type             STRING NOT NULL,
  requester_email          STRING NOT NULL,
  beneficiary_email        STRING NOT NULL,
  group_code               STRING NOT NULL,
  extends_grant_id         STRING,
  classification_code      STRING NOT NULL,
  justification            STRING NOT NULL,
  incident_ref             STRING,
  requested_duration_days  INT,
  status                   STRING NOT NULL,
  current_stage_no         INT,
  submitted_at             TIMESTAMP,
  decided_at               TIMESTAMP,
  fulfilled_at             TIMESTAMP,
  version                  INT NOT NULL,
  created_at               TIMESTAMP NOT NULL,
  updated_at               TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE access_request ADD CONSTRAINT access_request_type_ck CHECK (request_type IN ('GRANT','EXTEND','BREAK_GLASS'));
ALTER TABLE access_request ADD CONSTRAINT access_request_status_ck
  CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','PROVISIONING','FULFILLED','PROVISIONING_FAILED',
                    'REJECTED','CANCELLED'));
ALTER TABLE access_request ADD CONSTRAINT access_request_justification_ck CHECK (length(trim(justification)) >= 20);
ALTER TABLE access_request ADD CONSTRAINT access_request_bg_ck CHECK (request_type <> 'BREAK_GLASS' OR incident_ref IS NOT NULL);
ALTER TABLE access_request ADD CONSTRAINT access_request_extend_ck CHECK (request_type <> 'EXTEND' OR extends_grant_id IS NOT NULL);

CREATE TABLE IF NOT EXISTS approval_task (
  task_id             STRING NOT NULL COMMENT 'key',
  request_id          STRING NOT NULL,
  stage_no            INT NOT NULL,
  approver_role       STRING NOT NULL,
  status              STRING NOT NULL,
  is_post_hoc         BOOLEAN NOT NULL,
  due_at              TIMESTAMP NOT NULL,
  escalated_at        TIMESTAMP,
  decided_by_email    STRING,
  on_behalf_of_email  STRING,
  decided_at          TIMESTAMP,
  decision_comment    STRING,
  created_at          TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE approval_task ADD CONSTRAINT approval_task_status_ck
  CHECK (status IN ('PENDING','APPROVED','REJECTED','CANCELLED','EXPIRED'));
ALTER TABLE approval_task ADD CONSTRAINT approval_task_reject_ck
  CHECK (status <> 'REJECTED' OR length(trim(coalesce(decision_comment, ''))) >= 5);

CREATE TABLE IF NOT EXISTS access_grant (
  grant_id             STRING NOT NULL COMMENT 'key',
  request_id           STRING NOT NULL,
  user_email           STRING NOT NULL,
  group_code           STRING NOT NULL,
  status               STRING NOT NULL,
  is_break_glass       BOOLEAN NOT NULL,
  valid_from           TIMESTAMP NOT NULL,
  valid_to             TIMESTAMP NOT NULL,
  provisioned_at       TIMESTAMP,
  revoke_requested_at  TIMESTAMP,
  revoked_at           TIMESTAMP,
  revoke_reason        STRING,
  revoked_by           STRING,
  last_recertified_at  TIMESTAMP,
  expiry_notice_level  INT NOT NULL,
  created_at           TIMESTAMP NOT NULL,
  updated_at           TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE access_grant ADD CONSTRAINT access_grant_status_ck
  CHECK (status IN ('PENDING_PROVISION','ACTIVE','PENDING_REVOKE','REVOKED','EXPIRED','FAILED'));
ALTER TABLE access_grant ADD CONSTRAINT access_grant_dates_ck CHECK (valid_to > valid_from);
ALTER TABLE access_grant ADD CONSTRAINT access_grant_revoke_ck CHECK (status <> 'PENDING_REVOKE' OR revoke_reason IS NOT NULL);

-- ---------------------------------------------------------------------------
-- Provisioning queue (desired state -> Entra ID and Unity Catalog)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provisioning_action (
  action_id        STRING NOT NULL COMMENT 'key',
  action_type      STRING NOT NULL,
  grant_id         STRING,
  entitlement_id   STRING,
  group_code       STRING,
  idempotency_key  STRING NOT NULL COMMENT 'unique',
  status           STRING NOT NULL,
  attempts         INT NOT NULL,
  next_attempt_at  TIMESTAMP NOT NULL,
  started_at       TIMESTAMP,
  completed_at     TIMESTAMP,
  last_error       STRING,
  created_by       STRING NOT NULL,
  created_at       TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE provisioning_action ADD CONSTRAINT provisioning_action_type_ck
  CHECK (action_type IN ('ACTIVATE_GROUP','ADD_MEMBER','REMOVE_MEMBER','APPLY_ENTITLEMENT','REVOKE_ENTITLEMENT'));
ALTER TABLE provisioning_action ADD CONSTRAINT provisioning_action_status_ck
  CHECK (status IN ('QUEUED','IN_PROGRESS','SUCCEEDED','FAILED','DEAD','CANCELLED'));

-- ---------------------------------------------------------------------------
-- Recertification
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recert_campaign (
  campaign_id    STRING NOT NULL COMMENT 'key',
  name           STRING NOT NULL,
  campaign_type  STRING NOT NULL,
  opened_at      TIMESTAMP NOT NULL,
  due_at         TIMESTAMP NOT NULL,
  closed_at      TIMESTAMP,
  status         STRING NOT NULL,
  created_by     STRING NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE recert_campaign ADD CONSTRAINT recert_campaign_ck
  CHECK (campaign_type IN ('PERIODIC','MOVER','AD_HOC') AND status IN ('OPEN','CLOSED'));

CREATE TABLE IF NOT EXISTS recert_item (
  item_id           STRING NOT NULL COMMENT 'key',
  campaign_id       STRING NOT NULL,
  grant_id          STRING NOT NULL COMMENT 'one PENDING item per grant',
  reviewer_email    STRING NOT NULL,
  decision          STRING NOT NULL,
  decided_by        STRING,
  decided_at        TIMESTAMP,
  decision_comment  STRING,
  created_at        TIMESTAMP NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE recert_item ADD CONSTRAINT recert_item_decision_ck
  CHECK (decision IN ('PENDING','KEEP','REVOKE','NOT_ATTESTED'));

-- ---------------------------------------------------------------------------
-- Drift detection
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS drift_run (
  run_id       STRING NOT NULL COMMENT 'key',
  started_at   TIMESTAMP NOT NULL,
  finished_at  TIMESTAMP,
  status       STRING NOT NULL,
  summary      STRING COMMENT 'JSON'
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');

CREATE TABLE IF NOT EXISTS drift_finding (
  finding_id         STRING NOT NULL COMMENT 'key',
  run_id             STRING NOT NULL,
  finding_type       STRING NOT NULL,
  severity           STRING NOT NULL,
  fingerprint        STRING NOT NULL COMMENT 'one open finding per fingerprint',
  securable          STRING,
  principal          STRING,
  privilege          STRING,
  details            STRING COMMENT 'JSON',
  status             STRING NOT NULL,
  first_detected_at  TIMESTAMP NOT NULL,
  last_detected_at   TIMESTAMP NOT NULL,
  resolved_at        TIMESTAMP,
  resolved_by        STRING,
  resolution_note    STRING
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE drift_finding ADD CONSTRAINT drift_finding_ck
  CHECK (severity IN ('LOW','MEDIUM','HIGH')
         AND status IN ('OPEN','ACKNOWLEDGED','ACCEPTED_RISK','REMEDIATED','RESOLVED'));

-- ---------------------------------------------------------------------------
-- Notifications (outbox, sent by the jobs)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_outbox (
  notification_id  STRING NOT NULL COMMENT 'key',
  channel          STRING NOT NULL,
  recipients       STRING COMMENT 'comma-separated emails',
  subject          STRING NOT NULL,
  body             STRING NOT NULL,
  template         STRING NOT NULL,
  dedupe_key       STRING COMMENT 'unique when set',
  request_id       STRING,
  status           STRING NOT NULL,
  attempts         INT NOT NULL,
  last_error       STRING,
  created_at       TIMESTAMP NOT NULL,
  sent_at          TIMESTAMP
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.enableDeletionVectors' = 'true');
ALTER TABLE notification_outbox ADD CONSTRAINT notification_outbox_ck
  CHECK (channel IN ('EMAIL','TEAMS') AND status IN ('QUEUED','SENT','FAILED','DEAD'));

-- ---------------------------------------------------------------------------
-- Audit: append-only, hash-chained (tamper evident). See audit.py.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_event (
  chain_seq       BIGINT NOT NULL COMMENT 'key, 1..n without gaps',
  occurred_at     TIMESTAMP NOT NULL,
  actor           STRING NOT NULL,
  actor_type      STRING NOT NULL,
  action          STRING NOT NULL,
  object_kind     STRING NOT NULL,
  object_id       STRING,
  correlation_id  STRING,
  before_state    STRING COMMENT 'JSON',
  after_state     STRING COMMENT 'JSON',
  reason          STRING,
  prev_hash       STRING NOT NULL,
  event_hash      STRING NOT NULL
) TBLPROPERTIES ('delta.isolationLevel' = 'Serializable', 'delta.appendOnly' = 'true',
                 'delta.deletedFileRetentionDuration' = 'interval 30 days');
ALTER TABLE audit_event ADD CONSTRAINT audit_event_actor_ck CHECK (actor_type IN ('USER','SERVICE_PRINCIPAL','SYSTEM'));
