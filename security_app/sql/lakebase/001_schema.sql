-- =============================================================================
-- Security Access Workflow - operational store (Lakebase / PostgreSQL 16)
-- Migration 001: core schema. Applied once by jobs/migrate.py (tracked in
-- cdp_security.schema_migration). Never edit after it has been applied to tst/prd;
-- add a new numbered migration instead.
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS cdp_security;
SET search_path = cdp_security, public;

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------------
CREATE TABLE classification (
  classification_code   text PRIMARY KEY
                        CHECK (classification_code IN ('GENERAL','CONFIDENTIAL','RESTRICTED')),
  rank                  smallint NOT NULL UNIQUE,
  max_grant_days        integer  NOT NULL CHECK (max_grant_days > 0),
  recert_interval_days  integer  NOT NULL CHECK (recert_interval_days > 0),
  requires_project      boolean  NOT NULL DEFAULT false,
  description           text
);

CREATE TABLE app_user (
  user_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email            text NOT NULL UNIQUE CHECK (email = lower(email) AND email LIKE '%@%'),
  entra_object_id  text UNIQUE,
  display_name     text,
  department       text,
  is_active        boolean NOT NULL DEFAULT true,
  last_seen_at     timestamptz,
  deactivated_at   timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_app_user_upd BEFORE UPDATE ON app_user
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Fallback / bootstrap role assignment. Entra groups are the primary source
-- (see CDP_SECURITY_*_GROUP settings); this table exists so the first admin can
-- be seeded and so roles still work if on-behalf-of tokens are disabled.
CREATE TABLE app_role_member (
  email       text NOT NULL CHECK (email = lower(email)),
  app_role    text NOT NULL CHECK (app_role IN ('ADMIN','SECURITY','AUDITOR','BREAK_GLASS')),
  granted_by  text NOT NULL,
  granted_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (email, app_role)
);

CREATE TABLE workspace (
  workspace_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name                     text NOT NULL UNIQUE,
  workspace_type           text NOT NULL
                           CHECK (workspace_type IN ('INGESTION','PROJECT','DBANALYTICS','ANALYTICS_PBI','MLOPS')),
  host_url                 text NOT NULL UNIQUE CHECK (host_url ~ '^https://'),
  databricks_workspace_id  bigint,
  environment              text NOT NULL CHECK (environment IN ('dev','tst','prd')),
  is_active                boolean NOT NULL DEFAULT true,
  created_by               text NOT NULL,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_workspace_upd BEFORE UPDATE ON workspace
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE data_product (
  data_product_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code                 text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9_]{1,40}$'),
  name                 text NOT NULL,
  description          text,
  catalog_name         text NOT NULL CHECK (catalog_name = lower(catalog_name)),
  schema_name          text NOT NULL CHECK (schema_name = lower(schema_name)),
  classification_code  text NOT NULL REFERENCES classification,
  lifecycle_status     text NOT NULL DEFAULT 'ACTIVE'
                       CHECK (lifecycle_status IN ('PROPOSED','ACTIVE','DEPRECATED','RETIRED')),
  home_workspace_id    uuid REFERENCES workspace,
  created_by           text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (catalog_name, schema_name)
);
CREATE TRIGGER trg_dp_upd BEFORE UPDATE ON data_product
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE project (
  project_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code             text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9_]{1,40}$'),
  name             text NOT NULL,
  description      text,
  sponsor_user_id  uuid NOT NULL REFERENCES app_user,
  workspace_id     uuid REFERENCES workspace,
  start_date       date NOT NULL DEFAULT current_date,
  end_date         date,
  status           text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('PROPOSED','ACTIVE','CLOSED')),
  created_by       text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (end_date IS NULL OR end_date >= start_date)
);
CREATE TRIGGER trg_project_upd BEFORE UPDATE ON project
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Projects <-> data products (many-to-many)
CREATE TABLE project_data_product (
  project_id       uuid NOT NULL REFERENCES project,
  data_product_id  uuid NOT NULL REFERENCES data_product,
  justification    text NOT NULL,
  linked_by        text NOT NULL,
  linked_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, data_product_id)
);

CREATE TABLE logical_group (
  logical_group_code  text PRIMARY KEY CHECK (logical_group_code ~ '^[A-Z][A-Z0-9_]{1,30}$'),
  name                text NOT NULL,
  description         text
);

CREATE TABLE uc_securable (
  securable_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  securable_type       text NOT NULL CHECK (securable_type IN ('CATALOG','SCHEMA','TABLE','VOLUME','FUNCTION')),
  full_name            text NOT NULL UNIQUE CHECK (full_name = lower(full_name)),
  data_product_id      uuid REFERENCES data_product,
  classification_code  text NOT NULL DEFAULT 'GENERAL' REFERENCES classification,
  created_at           timestamptz NOT NULL DEFAULT now()
);

-- A securable (normally a table) can belong to many logical groups
CREATE TABLE securable_logical_group (
  securable_id        uuid NOT NULL REFERENCES uc_securable,
  logical_group_code  text NOT NULL REFERENCES logical_group,
  PRIMARY KEY (securable_id, logical_group_code)
);

CREATE TABLE ai_use_case (
  ai_use_case_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code                 text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9_]{1,40}$'),
  name                 text NOT NULL,
  description          text,
  data_product_id      uuid REFERENCES data_product,
  classification_code  text NOT NULL REFERENCES classification,
  status               text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','RETIRED')),
  created_by           text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_ai_upd BEFORE UPDATE ON ai_use_case
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Non-UC assets: Genie spaces, serving endpoints (incl. AI Function / agent
-- endpoints), AI/BI dashboards, Power BI workspaces and semantic models.
CREATE TABLE asset (
  asset_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_type           text NOT NULL CHECK (asset_type IN ('GENIE_SPACE','SERVING_ENDPOINT','AI_FUNCTION','AGENT',
                                                           'DASHBOARD','PBI_WORKSPACE','PBI_SEMANTIC_MODEL')),
  name                 text NOT NULL,
  -- Genie space id | endpoint name | dashboard id | PBI workspace GUID | "<pbiWorkspaceGuid>/<datasetGuid>"
  external_id          text NOT NULL,
  workspace_id         uuid REFERENCES workspace,
  data_product_id      uuid REFERENCES data_product,
  ai_use_case_id       uuid REFERENCES ai_use_case,
  project_id           uuid REFERENCES project,
  classification_code  text NOT NULL DEFAULT 'GENERAL' REFERENCES classification,
  is_active            boolean NOT NULL DEFAULT true,
  created_by           text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (asset_type, external_id),
  CHECK (asset_type LIKE 'PBI\_%' OR workspace_id IS NOT NULL)
);

-- Entra ID security groups managed through the app
CREATE TABLE access_group (
  access_group_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_name           text NOT NULL UNIQUE,
  entra_object_id      text UNIQUE,
  purpose              text NOT NULL CHECK (purpose IN ('DATA_PRODUCT','PROJECT','AI_USE_CASE','WORKSPACE')),
  privilege_level      text NOT NULL CHECK (privilege_level IN ('READ','WRITE','CONTRIB','USER','ADMIN')),
  data_product_id      uuid REFERENCES data_product,
  project_id           uuid REFERENCES project,
  ai_use_case_id       uuid REFERENCES ai_use_case,
  workspace_id         uuid REFERENCES workspace,
  classification_code  text NOT NULL REFERENCES classification,
  owner_user_id        uuid REFERENCES app_user,
  status               text NOT NULL DEFAULT 'PLANNED'
                       CHECK (status IN ('PLANNED','ACTIVE','RETIRING','RETIRED')),
  requestable          boolean NOT NULL DEFAULT true,
  description          text,
  created_by           text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (purpose <> 'DATA_PRODUCT' OR data_product_id IS NOT NULL),
  CHECK (purpose <> 'PROJECT'      OR project_id      IS NOT NULL),
  CHECK (purpose <> 'AI_USE_CASE'  OR ai_use_case_id  IS NOT NULL),
  CHECK (purpose <> 'WORKSPACE'    OR workspace_id    IS NOT NULL),
  CHECK (status <> 'ACTIVE' OR entra_object_id IS NOT NULL)
);
CREATE TRIGGER trg_group_upd BEFORE UPDATE ON access_group
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Desired state: what each group is entitled to. Exactly one target.
CREATE TABLE group_entitlement (
  entitlement_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  access_group_id  uuid NOT NULL REFERENCES access_group,
  securable_id     uuid REFERENCES uc_securable,
  asset_id         uuid REFERENCES asset,
  privilege        text NOT NULL,
  desired_state    text NOT NULL DEFAULT 'PRESENT' CHECK (desired_state IN ('PRESENT','ABSENT')),
  apply_status     text NOT NULL DEFAULT 'PENDING' CHECK (apply_status IN ('PENDING','APPLIED','REMOVED','FAILED')),
  last_applied_at  timestamptz,
  last_error       text,
  created_by       text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(securable_id, asset_id) = 1)
);
CREATE UNIQUE INDEX ux_entitlement ON group_entitlement (access_group_id, coalesce(securable_id, asset_id), privilege);
CREATE TRIGGER trg_ent_upd BEFORE UPDATE ON group_entitlement
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Owners / stewards of any governed object (polymorphic by design)
CREATE TABLE ownership (
  ownership_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  object_kind   text NOT NULL CHECK (object_kind IN ('DATA_PRODUCT','PROJECT','AI_USE_CASE','ACCESS_GROUP','ASSET')),
  object_id     uuid NOT NULL,
  user_id       uuid NOT NULL REFERENCES app_user,
  role          text NOT NULL CHECK (role IN ('DATA_OWNER','STEWARD','TECHNICAL_OWNER')),
  valid_from    timestamptz NOT NULL DEFAULT now(),
  valid_to      timestamptz,
  assigned_by   text NOT NULL,
  CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE UNIQUE INDEX ux_ownership_active ON ownership (object_kind, object_id, user_id, role) WHERE valid_to IS NULL;
CREATE INDEX ix_ownership_object ON ownership (object_kind, object_id) WHERE valid_to IS NULL;

-- Approval route per classification. Changed only through migrations
-- (i.e. through design governance), never through the UI.
CREATE TABLE approval_rule (
  classification_code  text NOT NULL REFERENCES classification,
  stage_no             smallint NOT NULL CHECK (stage_no BETWEEN 1 AND 5),
  approver_role        text NOT NULL CHECK (approver_role IN ('DATA_OWNER','STEWARD','SECURITY','PROJECT_SPONSOR')),
  sla_hours            integer NOT NULL CHECK (sla_hours > 0),
  PRIMARY KEY (classification_code, stage_no)
);

CREATE TABLE delegation (
  delegation_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  delegator_user_id  uuid NOT NULL REFERENCES app_user,
  delegate_user_id   uuid NOT NULL REFERENCES app_user,
  valid_from         timestamptz NOT NULL DEFAULT now(),
  valid_to           timestamptz NOT NULL,
  reason             text NOT NULL,
  created_by         text NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (delegator_user_id <> delegate_user_id),
  CHECK (valid_to > valid_from),
  CHECK (valid_to <= valid_from + interval '60 days')
);

-- ---------------------------------------------------------------------------
-- Workflow
-- ---------------------------------------------------------------------------
CREATE SEQUENCE access_request_ref_seq;

CREATE TABLE access_request (
  request_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  request_ref              text NOT NULL UNIQUE
                           DEFAULT ('SAR-' || lpad(nextval('access_request_ref_seq')::text, 7, '0')),
  request_type             text NOT NULL CHECK (request_type IN ('GRANT','EXTEND','BREAK_GLASS')),
  requester_user_id        uuid NOT NULL REFERENCES app_user,
  beneficiary_user_id      uuid NOT NULL REFERENCES app_user,
  access_group_id          uuid NOT NULL REFERENCES access_group,
  project_id               uuid REFERENCES project,
  extends_grant_id         uuid,
  classification_code      text NOT NULL REFERENCES classification,
  justification            text NOT NULL CHECK (length(btrim(justification)) >= 20),
  incident_ref             text,
  requested_duration_days  integer CHECK (requested_duration_days > 0),
  status                   text NOT NULL CHECK (status IN ('DRAFT','PENDING_APPROVAL','APPROVED','PROVISIONING',
                                                           'FULFILLED','PROVISIONING_FAILED','REJECTED','CANCELLED')),
  current_stage_no         smallint,
  submitted_at             timestamptz,
  decided_at               timestamptz,
  fulfilled_at             timestamptz,
  version                  integer NOT NULL DEFAULT 1,
  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now(),
  CHECK (request_type <> 'BREAK_GLASS' OR incident_ref IS NOT NULL),
  CHECK (request_type <> 'EXTEND' OR extends_grant_id IS NOT NULL)
);
CREATE UNIQUE INDEX ux_request_open_grant ON access_request (beneficiary_user_id, access_group_id)
  WHERE request_type = 'GRANT' AND status IN ('DRAFT','PENDING_APPROVAL','APPROVED','PROVISIONING');
CREATE UNIQUE INDEX ux_request_open_extend ON access_request (extends_grant_id)
  WHERE request_type = 'EXTEND' AND status IN ('DRAFT','PENDING_APPROVAL');
CREATE INDEX ix_request_status ON access_request (status);
CREATE TRIGGER trg_request_upd BEFORE UPDATE ON access_request
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE approval_task (
  task_id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id            uuid NOT NULL REFERENCES access_request,
  stage_no              smallint NOT NULL,
  approver_role         text NOT NULL CHECK (approver_role IN ('DATA_OWNER','STEWARD','SECURITY','PROJECT_SPONSOR')),
  status                text NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','APPROVED','REJECTED','CANCELLED','EXPIRED')),
  is_post_hoc           boolean NOT NULL DEFAULT false,
  due_at                timestamptz NOT NULL,
  escalated_at          timestamptz,
  decided_by_user_id    uuid REFERENCES app_user,
  on_behalf_of_user_id  uuid REFERENCES app_user,
  decided_at            timestamptz,
  decision_comment      text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (request_id, stage_no),
  CHECK (status <> 'REJECTED' OR length(btrim(coalesce(decision_comment, ''))) >= 5)
);
CREATE INDEX ix_task_pending ON approval_task (due_at) WHERE status = 'PENDING';

CREATE TABLE access_grant (
  grant_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id           uuid NOT NULL REFERENCES access_request,
  user_id              uuid NOT NULL REFERENCES app_user,
  access_group_id      uuid NOT NULL REFERENCES access_group,
  status               text NOT NULL DEFAULT 'PENDING_PROVISION'
                       CHECK (status IN ('PENDING_PROVISION','ACTIVE','PENDING_REVOKE','REVOKED','EXPIRED','FAILED')),
  is_break_glass       boolean NOT NULL DEFAULT false,
  valid_from           timestamptz NOT NULL DEFAULT now(),
  valid_to             timestamptz NOT NULL,
  provisioned_at       timestamptz,
  revoke_requested_at  timestamptz,
  revoked_at           timestamptz,
  revoke_reason        text CHECK (revoke_reason IS NULL OR revoke_reason IN
                         ('EXPIRED','SELF','OWNER','ADMIN','RECERT_REVOKE','RECERT_NOT_ATTESTED',
                          'LEAVER','POST_HOC_REJECTED','GROUP_RETIRED')),
  revoked_by           text,
  last_recertified_at  timestamptz,
  expiry_notice_level  smallint NOT NULL DEFAULT 0,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CHECK (valid_to > valid_from),
  CHECK (status <> 'PENDING_REVOKE' OR revoke_reason IS NOT NULL)
);
CREATE UNIQUE INDEX ux_grant_live ON access_grant (user_id, access_group_id)
  WHERE status IN ('PENDING_PROVISION','ACTIVE','PENDING_REVOKE');
CREATE INDEX ix_grant_expiry ON access_grant (valid_to) WHERE status = 'ACTIVE';
CREATE TRIGGER trg_grant_upd BEFORE UPDATE ON access_grant
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE access_request
  ADD CONSTRAINT fk_request_extends_grant FOREIGN KEY (extends_grant_id) REFERENCES access_grant (grant_id);

-- ---------------------------------------------------------------------------
-- Provisioning queue (desired state -> real systems)
-- ---------------------------------------------------------------------------
CREATE TABLE provisioning_action (
  action_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  action_type      text NOT NULL CHECK (action_type IN ('CREATE_GROUP','ADD_MEMBER','REMOVE_MEMBER',
                                                        'APPLY_ENTITLEMENT','REVOKE_ENTITLEMENT')),
  grant_id         uuid REFERENCES access_grant,
  entitlement_id   uuid REFERENCES group_entitlement,
  access_group_id  uuid REFERENCES access_group,
  idempotency_key  text NOT NULL UNIQUE,
  status           text NOT NULL DEFAULT 'QUEUED'
                   CHECK (status IN ('QUEUED','IN_PROGRESS','SUCCEEDED','FAILED','DEAD','CANCELLED')),
  attempts         integer NOT NULL DEFAULT 0,
  next_attempt_at  timestamptz NOT NULL DEFAULT now(),
  started_at       timestamptz,
  completed_at     timestamptz,
  last_error       text,
  created_by       text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_action_due ON provisioning_action (next_attempt_at) WHERE status IN ('QUEUED','FAILED');

-- ---------------------------------------------------------------------------
-- Recertification
-- ---------------------------------------------------------------------------
CREATE TABLE recert_campaign (
  campaign_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text NOT NULL UNIQUE,
  campaign_type  text NOT NULL CHECK (campaign_type IN ('PERIODIC','MOVER','AD_HOC')),
  opened_at      timestamptz NOT NULL DEFAULT now(),
  due_at         timestamptz NOT NULL,
  closed_at      timestamptz,
  status         text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','CLOSED')),
  created_by     text NOT NULL
);

CREATE TABLE recert_item (
  item_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id       uuid NOT NULL REFERENCES recert_campaign,
  grant_id          uuid NOT NULL REFERENCES access_grant,
  reviewer_user_id  uuid NOT NULL REFERENCES app_user,
  decision          text NOT NULL DEFAULT 'PENDING' CHECK (decision IN ('PENDING','KEEP','REVOKE','NOT_ATTESTED')),
  decided_by        text,
  decided_at        timestamptz,
  decision_comment  text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (campaign_id, grant_id)
);
CREATE UNIQUE INDEX ux_recert_open_grant ON recert_item (grant_id) WHERE decision = 'PENDING';

-- ---------------------------------------------------------------------------
-- Drift detection
-- ---------------------------------------------------------------------------
CREATE TABLE drift_run (
  run_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  status       text NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','SUCCEEDED','FAILED')),
  summary      jsonb
);

CREATE TABLE drift_finding (
  finding_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id             uuid NOT NULL REFERENCES drift_run,
  finding_type       text NOT NULL CHECK (finding_type IN ('MISSING_GRANT','UNEXPECTED_GRANT','DIRECT_USER_GRANT',
                                                          'MISSING_MEMBER','UNEXPECTED_MEMBER','OUT_OF_BAND_CHANGE')),
  severity           text NOT NULL DEFAULT 'MEDIUM' CHECK (severity IN ('LOW','MEDIUM','HIGH')),
  fingerprint        text NOT NULL,
  securable          text,
  principal          text,
  privilege          text,
  details            jsonb,
  status             text NOT NULL DEFAULT 'OPEN'
                     CHECK (status IN ('OPEN','ACKNOWLEDGED','ACCEPTED_RISK','REMEDIATED','RESOLVED')),
  first_detected_at  timestamptz NOT NULL DEFAULT now(),
  last_detected_at   timestamptz NOT NULL DEFAULT now(),
  resolved_at        timestamptz,
  resolved_by        text,
  resolution_note    text
);
CREATE UNIQUE INDEX ux_drift_open ON drift_finding (fingerprint)
  WHERE status IN ('OPEN','ACKNOWLEDGED','ACCEPTED_RISK');

-- ---------------------------------------------------------------------------
-- Notifications (transactional outbox)
-- ---------------------------------------------------------------------------
CREATE TABLE notification_outbox (
  notification_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel          text NOT NULL CHECK (channel IN ('EMAIL','TEAMS')),
  recipients       text[] NOT NULL,
  subject          text NOT NULL,
  body             text NOT NULL,
  template         text NOT NULL,
  dedupe_key       text UNIQUE,
  request_id       uuid REFERENCES access_request,
  status           text NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','SENT','FAILED','DEAD')),
  attempts         integer NOT NULL DEFAULT 0,
  last_error       text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  sent_at          timestamptz
);
CREATE INDEX ix_outbox_queued ON notification_outbox (created_at) WHERE status IN ('QUEUED','FAILED');

-- ---------------------------------------------------------------------------
-- Audit: append-only, hash-chained (tamper evident)
-- ---------------------------------------------------------------------------
CREATE TABLE audit_event (
  event_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  chain_seq       bigint NOT NULL UNIQUE,
  occurred_at     timestamptz NOT NULL,
  actor           text NOT NULL,
  actor_type      text NOT NULL CHECK (actor_type IN ('USER','SERVICE_PRINCIPAL','SYSTEM')),
  action          text NOT NULL,
  object_kind     text NOT NULL,
  object_id       text,
  correlation_id  text,
  before_state    jsonb,
  after_state     jsonb,
  reason          text,
  prev_hash       text NOT NULL,
  event_hash      text NOT NULL
);
CREATE INDEX ix_audit_object ON audit_event (object_kind, object_id);
CREATE INDEX ix_audit_actor  ON audit_event (actor, occurred_at);
CREATE INDEX ix_audit_corr   ON audit_event (correlation_id);

CREATE OR REPLACE FUNCTION audit_event_hash(
  p_seq bigint, p_prev text, p_at timestamptz, p_actor text, p_actor_type text, p_action text,
  p_kind text, p_obj text, p_corr text, p_before jsonb, p_after jsonb, p_reason text
) RETURNS text LANGUAGE sql STABLE AS $$
  SELECT encode(sha256(convert_to(concat_ws('|',
           p_seq::text, p_prev,
           to_char(p_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
           p_actor, p_actor_type, p_action, p_kind,
           coalesce(p_obj, ''), coalesce(p_corr, ''),
           coalesce(p_before::text, ''), coalesce(p_after::text, ''), coalesce(p_reason, '')
         ), 'UTF8')), 'hex')
$$;

CREATE OR REPLACE FUNCTION audit_event_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_prev_seq  bigint;
  v_prev_hash text;
BEGIN
  -- Serialise writers so the chain is strictly linear. Held until commit.
  PERFORM pg_advisory_xact_lock(7340021);
  SELECT chain_seq, event_hash INTO v_prev_seq, v_prev_hash
    FROM audit_event ORDER BY chain_seq DESC LIMIT 1;
  NEW.chain_seq   := coalesce(v_prev_seq, 0) + 1;
  NEW.prev_hash   := coalesce(v_prev_hash, 'GENESIS');
  NEW.occurred_at := clock_timestamp();
  NEW.event_hash  := audit_event_hash(NEW.chain_seq, NEW.prev_hash, NEW.occurred_at, NEW.actor, NEW.actor_type,
                                      NEW.action, NEW.object_kind, NEW.object_id, NEW.correlation_id,
                                      NEW.before_state, NEW.after_state, NEW.reason);
  RETURN NEW;
END $$;

CREATE TRIGGER trg_audit_chain BEFORE INSERT ON audit_event
  FOR EACH ROW EXECUTE FUNCTION audit_event_chain();

CREATE OR REPLACE FUNCTION audit_event_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_event is append-only (% blocked)', TG_OP;
END $$;

CREATE TRIGGER trg_audit_no_update BEFORE UPDATE OR DELETE ON audit_event
  FOR EACH ROW EXECUTE FUNCTION audit_event_immutable();
CREATE TRIGGER trg_audit_no_truncate BEFORE TRUNCATE ON audit_event
  FOR EACH STATEMENT EXECUTE FUNCTION audit_event_immutable();

-- Returns rows only when the chain is broken (empty result = intact)
CREATE OR REPLACE FUNCTION verify_audit_chain()
RETURNS TABLE (broken_at_seq bigint, problem text) LANGUAGE sql STABLE AS $$
  WITH ordered AS (
    SELECT e.*, lag(e.event_hash) OVER (ORDER BY e.chain_seq) AS lag_hash,
                lag(e.chain_seq)  OVER (ORDER BY e.chain_seq) AS lag_seq
    FROM audit_event e
  )
  SELECT chain_seq,
         CASE
           WHEN event_hash <> audit_event_hash(chain_seq, prev_hash, occurred_at, actor, actor_type, action,
                                               object_kind, object_id, correlation_id, before_state, after_state, reason)
             THEN 'event hash does not match content'
           WHEN prev_hash <> coalesce(lag_hash, 'GENESIS') THEN 'prev_hash does not match previous event'
           ELSE 'sequence gap'
         END
  FROM ordered
  WHERE event_hash <> audit_event_hash(chain_seq, prev_hash, occurred_at, actor, actor_type, action,
                                       object_kind, object_id, correlation_id, before_state, after_state, reason)
     OR prev_hash <> coalesce(lag_hash, 'GENESIS')
     OR chain_seq <> coalesce(lag_seq, 0) + 1
  ORDER BY chain_seq
  LIMIT 100
$$;

-- ---------------------------------------------------------------------------
-- Read views used by the app
-- ---------------------------------------------------------------------------
CREATE VIEW v_group AS
SELECT g.*,
       dp.code AS data_product_code, dp.name AS data_product_name,
       uc.code AS ai_use_case_code,  uc.name AS ai_use_case_name,
       p.code  AS project_code,      p.name  AS project_name,
       w.name  AS workspace_name,
       coalesce(g.data_product_id, uc.data_product_id) AS effective_data_product_id,
       ou.email AS owner_email
FROM access_group g
LEFT JOIN ai_use_case uc ON uc.ai_use_case_id = g.ai_use_case_id
LEFT JOIN data_product dp ON dp.data_product_id = coalesce(g.data_product_id, uc.data_product_id)
LEFT JOIN project p ON p.project_id = g.project_id
LEFT JOIN workspace w ON w.workspace_id = g.workspace_id
LEFT JOIN app_user ou ON ou.user_id = g.owner_user_id;

CREATE VIEW v_request AS
SELECT r.request_id, r.request_ref, r.request_type, r.status, r.classification_code, r.current_stage_no,
       r.justification, r.incident_ref, r.requested_duration_days, r.extends_grant_id,
       r.submitted_at, r.decided_at, r.fulfilled_at, r.created_at,
       rq.email AS requester_email, bn.email AS beneficiary_email,
       g.access_group_id, g.group_name, g.purpose, g.privilege_level,
       g.data_product_code, g.data_product_name, g.ai_use_case_code,
       p.code AS project_code, p.name AS project_name,
       (SELECT max(ar.stage_no) FROM approval_rule ar WHERE ar.classification_code = r.classification_code) AS total_stages
FROM access_request r
JOIN app_user rq ON rq.user_id = r.requester_user_id
JOIN app_user bn ON bn.user_id = r.beneficiary_user_id
JOIN v_group g   ON g.access_group_id = r.access_group_id
LEFT JOIN project p ON p.project_id = r.project_id;

CREATE VIEW v_grant AS
SELECT gr.grant_id, gr.request_id, gr.status, gr.is_break_glass, gr.valid_from, gr.valid_to,
       gr.provisioned_at, gr.revoked_at, gr.revoke_reason, gr.last_recertified_at,
       u.user_id, u.email AS user_email, u.display_name,
       g.access_group_id, g.group_name, g.purpose, g.privilege_level, g.classification_code,
       g.data_product_code, g.data_product_name, g.ai_use_case_code, g.project_code,
       r.request_ref
FROM access_grant gr
JOIN app_user u  ON u.user_id = gr.user_id
JOIN v_group g   ON g.access_group_id = gr.access_group_id
JOIN access_request r ON r.request_id = gr.request_id;

CREATE VIEW v_task AS
SELECT t.task_id, t.stage_no, t.approver_role, t.status AS task_status, t.is_post_hoc, t.due_at,
       t.escalated_at, t.decided_at AS task_decided_at, t.decision_comment,
       du.email AS decided_by_email, ob.email AS on_behalf_of_email,
       r.*
FROM approval_task t
JOIN v_request r ON r.request_id = t.request_id
LEFT JOIN app_user du ON du.user_id = t.decided_by_user_id
LEFT JOIN app_user ob ON ob.user_id = t.on_behalf_of_user_id;
