"""Read-only queries used by the UI. No authorisation decisions are made here;
pages only call the functions appropriate to the caller's role."""
from __future__ import annotations

from datetime import datetime

from .db import fetch_all, fetch_one

LIVE = ("PENDING_PROVISION", "ACTIVE", "PENDING_REVOKE")


def classifications() -> list[dict]:
    return fetch_all("SELECT * FROM classification ORDER BY rank")


def approval_rules(classification_code: str | None = None) -> list[dict]:
    if classification_code:
        return fetch_all("SELECT * FROM approval_rule WHERE classification_code = %s ORDER BY stage_no",
                         (classification_code,))
    return fetch_all("SELECT r.* FROM approval_rule r JOIN classification c USING (classification_code) "
                     "ORDER BY c.rank, r.stage_no")


def requestable_groups() -> list[dict]:
    return fetch_all("SELECT * FROM v_group WHERE requestable AND status IN ('PLANNED','ACTIVE') "
                     "ORDER BY purpose, group_name")


def active_groups() -> list[dict]:
    return fetch_all("SELECT * FROM v_group WHERE status = 'ACTIVE' ORDER BY group_name")


def projects_for_group(group: dict) -> list[dict]:
    if group["purpose"] == "PROJECT":
        return fetch_all("SELECT * FROM project WHERE project_id = %s", (group["project_id"],))
    if group.get("effective_data_product_id"):
        return fetch_all(
            "SELECT p.* FROM project p JOIN project_data_product l ON l.project_id = p.project_id "
            "WHERE l.data_product_id = %s AND p.status = 'ACTIVE' "
            "AND (p.end_date IS NULL OR p.end_date >= current_date) ORDER BY p.code",
            (group["effective_data_product_id"],))
    return fetch_all("SELECT * FROM project WHERE status = 'ACTIVE' ORDER BY code")


def my_grants(email: str) -> list[dict]:
    return fetch_all("SELECT * FROM v_grant WHERE user_email = %s AND status = ANY(%s) ORDER BY valid_to",
                     (email, list(LIVE)))


def my_requests(email: str) -> list[dict]:
    return fetch_all("SELECT * FROM v_request WHERE requester_email = %s OR beneficiary_email = %s "
                     "ORDER BY created_at DESC LIMIT 100", (email, email))


def my_decisions(email: str) -> list[dict]:
    return fetch_all("SELECT * FROM v_task WHERE decided_by_email = %s ORDER BY task_decided_at DESC LIMIT 50",
                     (email,))


def request_tasks(request_id) -> list[dict]:
    return fetch_all("SELECT stage_no, approver_role, task_status, due_at, decided_by_email, task_decided_at, "
                     "decision_comment FROM v_task WHERE request_id = %s ORDER BY stage_no", (request_id,))


def recert_items_for(email: str) -> list[dict]:
    return fetch_all(
        """
        SELECT i.item_id, c.name AS campaign, c.due_at, ru.email AS reviewer_email,
               g.user_email, g.group_name, g.classification_code, g.data_product_code, g.ai_use_case_code,
               g.valid_to, g.last_recertified_at, g.request_ref
        FROM recert_item i
        JOIN recert_campaign c ON c.campaign_id = i.campaign_id
        JOIN v_grant g ON g.grant_id = i.grant_id
        JOIN app_user ru ON ru.user_id = i.reviewer_user_id
        WHERE i.decision = 'PENDING'
          AND (ru.email = %s OR EXISTS (
                SELECT 1 FROM delegation d JOIN app_user de ON de.user_id = d.delegate_user_id
                WHERE d.delegator_user_id = i.reviewer_user_id AND de.email = %s
                  AND now() BETWEEN d.valid_from AND d.valid_to))
        ORDER BY c.due_at, g.group_name
        """, (email, email))


def delegations_for(email: str) -> list[dict]:
    return fetch_all(
        "SELECT d.delegation_id, a.email AS delegator, b.email AS delegate, d.valid_from, d.valid_to, d.reason "
        "FROM delegation d JOIN app_user a ON a.user_id = d.delegator_user_id "
        "JOIN app_user b ON b.user_id = d.delegate_user_id "
        "WHERE (a.email = %s OR b.email = %s) AND d.valid_to > now() ORDER BY d.valid_from", (email, email))


# ---- admin / reference --------------------------------------------------------
def workspaces() -> list[dict]:
    return fetch_all("SELECT * FROM workspace ORDER BY environment, workspace_type, name")


def data_products() -> list[dict]:
    return fetch_all("SELECT * FROM data_product ORDER BY code")


def projects() -> list[dict]:
    return fetch_all("SELECT p.*, u.email AS sponsor_email FROM project p "
                     "JOIN app_user u ON u.user_id = p.sponsor_user_id ORDER BY p.code")


def project_links() -> list[dict]:
    return fetch_all("SELECT p.code AS project, d.code AS data_product, l.justification, l.linked_by, l.linked_at "
                     "FROM project_data_product l JOIN project p USING (project_id) "
                     "JOIN data_product d USING (data_product_id) ORDER BY p.code, d.code")


def assets() -> list[dict]:
    return fetch_all("SELECT a.*, w.name AS workspace_name FROM asset a LEFT JOIN workspace w USING (workspace_id) "
                     "ORDER BY a.asset_type, a.name")


def ai_use_cases() -> list[dict]:
    return fetch_all("SELECT u.*, d.code AS data_product_code FROM ai_use_case u "
                     "LEFT JOIN data_product d USING (data_product_id) ORDER BY u.code")


def groups() -> list[dict]:
    return fetch_all("SELECT * FROM v_group ORDER BY status, group_name")


def entitlements(access_group_id) -> list[dict]:
    return fetch_all(
        "SELECT e.entitlement_id, coalesce(s.securable_type, a.asset_type) AS target_type, "
        "coalesce(s.full_name, a.name) AS target, e.privilege, e.desired_state, e.apply_status, "
        "e.last_applied_at, e.last_error FROM group_entitlement e "
        "LEFT JOIN uc_securable s USING (securable_id) LEFT JOIN asset a USING (asset_id) "
        "WHERE e.access_group_id = %s ORDER BY target_type, target, e.privilege", (access_group_id,))


def ownerships() -> list[dict]:
    return fetch_all(
        "SELECT o.ownership_id, o.object_kind, coalesce(d.code, p.code, u2.code, g.group_name, a.name) AS object, "
        "o.object_id, o.role, u.email, o.valid_from FROM ownership o JOIN app_user u USING (user_id) "
        "LEFT JOIN data_product d ON d.data_product_id = o.object_id "
        "LEFT JOIN project p ON p.project_id = o.object_id "
        "LEFT JOIN ai_use_case u2 ON u2.ai_use_case_id = o.object_id "
        "LEFT JOIN access_group g ON g.access_group_id = o.object_id "
        "LEFT JOIN asset a ON a.asset_id = o.object_id "
        "WHERE o.valid_to IS NULL ORDER BY o.object_kind, object, o.role")


def app_role_members() -> list[dict]:
    return fetch_all("SELECT * FROM app_role_member ORDER BY app_role, email")


def logical_groups() -> list[dict]:
    return fetch_all("SELECT * FROM logical_group ORDER BY logical_group_code")


# ---- operations / audit ---------------------------------------------------------
def actions(statuses: list[str], limit: int = 500) -> list[dict]:
    return fetch_all(
        "SELECT a.action_id, a.action_type, a.status, a.attempts, a.next_attempt_at, a.last_error, a.created_at, "
        "a.completed_at, g.group_name, u.email AS user_email FROM provisioning_action a "
        "LEFT JOIN access_group g ON g.access_group_id = a.access_group_id "
        "LEFT JOIN access_grant gr ON gr.grant_id = a.grant_id LEFT JOIN app_user u ON u.user_id = gr.user_id "
        "WHERE a.status = ANY(%s) ORDER BY a.created_at DESC LIMIT %s", (statuses, limit))


def queue_summary() -> list[dict]:
    return fetch_all("SELECT action_type, status, count(*) AS n FROM provisioning_action "
                     "WHERE created_at > now() - interval '30 days' GROUP BY 1, 2 ORDER BY 1, 2")


def failed_notifications() -> list[dict]:
    return fetch_all("SELECT notification_id, channel, subject, attempts, last_error, created_at "
                     "FROM notification_outbox WHERE status IN ('FAILED','DEAD') ORDER BY created_at DESC LIMIT 200")


def audit_events(since: datetime, until: datetime, actor: str | None, text: str | None, limit: int = 2000) -> list[dict]:
    sql = ("SELECT chain_seq, occurred_at, actor, actor_type, action, object_kind, object_id, correlation_id, "
           "reason, before_state::text AS before_state, after_state::text AS after_state, event_hash "
           "FROM audit_event WHERE occurred_at BETWEEN %s AND %s")
    params: list = [since, until]
    if actor:
        sql += " AND actor ILIKE %s"
        params.append(f"%{actor}%")
    if text:
        sql += " AND (object_id ILIKE %s OR correlation_id ILIKE %s OR action ILIKE %s)"
        params += [f"%{text}%"] * 3
    sql += " ORDER BY chain_seq DESC LIMIT %s"
    params.append(limit)
    return fetch_all(sql, params)


def drift_findings(statuses: list[str]) -> list[dict]:
    return fetch_all("SELECT finding_id, finding_type, severity, securable, principal, privilege, status, "
                     "first_detected_at, last_detected_at, resolution_note, details::text AS details "
                     "FROM drift_finding WHERE status = ANY(%s) ORDER BY severity DESC, first_detected_at",
                     (statuses,))


def last_drift_run() -> dict | None:
    return fetch_one("SELECT * FROM drift_run ORDER BY started_at DESC LIMIT 1")


def access_evidence() -> list[dict]:
    """Every live grant with its approval trail - recertification evidence pack."""
    return fetch_all(
        """
        SELECT g.user_email, g.group_name, g.classification_code, g.data_product_code, g.ai_use_case_code,
               g.project_code, g.status, g.is_break_glass, g.valid_from, g.valid_to, g.last_recertified_at,
               g.request_ref,
               (SELECT string_agg(t.approver_role || ':' || coalesce(t.decided_by_email, '-') || '@' ||
                                  coalesce(to_char(t.task_decided_at, 'YYYY-MM-DD'), '-'), '; ' ORDER BY t.stage_no)
                FROM v_task t WHERE t.request_id = g.request_id) AS approvals
        FROM v_grant g WHERE g.status = ANY(%s) ORDER BY g.group_name, g.user_email
        """, (list(LIVE),))


def owned_objects(email: str) -> list[dict]:
    """Data products and AI use cases this person owns or stewards (current assignments)."""
    return fetch_all(
        """
        SELECT o.object_kind, o.object_id, o.role,
               coalesce(d.code, a.code) AS code, coalesce(d.name, a.name) AS name,
               coalesce(d.classification_code, a.classification_code) AS classification_code
        FROM ownership o JOIN app_user u ON u.user_id = o.user_id
        LEFT JOIN data_product d ON o.object_kind = 'DATA_PRODUCT' AND d.data_product_id = o.object_id
        LEFT JOIN ai_use_case a ON o.object_kind = 'AI_USE_CASE' AND a.ai_use_case_id = o.object_id
        WHERE u.email = %s AND o.valid_to IS NULL AND o.role IN ('DATA_OWNER', 'STEWARD')
          AND o.object_kind IN ('DATA_PRODUCT', 'AI_USE_CASE')
        ORDER BY o.object_kind, code
        """, (email,))


def grants_for_owned(email: str) -> list[dict]:
    """Live grants on the data products / AI use cases this person owns or stewards."""
    return fetch_all(
        """
        SELECT g.* FROM v_grant g
        WHERE g.status = ANY(%s) AND EXISTS (
          SELECT 1 FROM ownership o JOIN app_user u ON u.user_id = o.user_id
          LEFT JOIN data_product d ON d.data_product_id = o.object_id
          LEFT JOIN ai_use_case a ON a.ai_use_case_id = o.object_id
          WHERE u.email = %s AND o.valid_to IS NULL AND o.role IN ('DATA_OWNER', 'STEWARD')
            AND (d.code = g.data_product_code OR a.code = g.ai_use_case_code))
        ORDER BY g.group_name, g.user_email
        """, (list(LIVE), email))


def group_members(access_group_id) -> list[dict]:
    return fetch_all("SELECT * FROM v_grant WHERE access_group_id = %s AND status = ANY(%s) ORDER BY user_email",
                     (access_group_id, list(LIVE)))
