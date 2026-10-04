"""Read-only queries used by the UI. No authorisation decisions are made here;
pages only call the functions appropriate to the caller's role."""
from __future__ import annotations

from datetime import datetime

from .db import fetch_all

LIVE = ["PENDING_PROVISION", "ACTIVE", "PENDING_REVOKE"]


def classifications() -> list[dict]:
    return fetch_all("SELECT * FROM classification ORDER BY rank")


def approval_rules(classification_code: str) -> list[dict]:
    return fetch_all("SELECT * FROM approval_rule WHERE classification_code = :c ORDER BY stage_no",
                     {"c": classification_code})


def requestable_groups() -> list[dict]:
    return fetch_all("SELECT * FROM v_group WHERE requestable AND status = 'ACTIVE' "
                     "AND data_product_status = 'ACTIVE' ORDER BY data_product_name, group_name")


def active_groups() -> list[dict]:
    return fetch_all("SELECT * FROM v_group WHERE status = 'ACTIVE' ORDER BY group_name")


def my_grants(email: str) -> list[dict]:
    return fetch_all("SELECT * FROM v_grant WHERE user_email = :e AND status IN (:live) ORDER BY valid_to",
                     {"e": email, "live": LIVE})


def my_requests(email: str) -> list[dict]:
    return fetch_all("SELECT * FROM v_request WHERE requester_email = :e OR beneficiary_email = :e "
                     "ORDER BY created_at DESC LIMIT 100", {"e": email})


def my_decisions(email: str) -> list[dict]:
    return fetch_all("SELECT * FROM v_task WHERE decided_by_email = :e ORDER BY task_decided_at DESC LIMIT 50",
                     {"e": email})


def request_tasks(request_id: str) -> list[dict]:
    return fetch_all("SELECT stage_no, approver_role, task_status, due_at, decided_by_email, task_decided_at, "
                     "decision_comment FROM v_task WHERE request_id = :id ORDER BY stage_no", {"id": request_id})


def recert_items_for(email: str) -> list[dict]:
    return fetch_all(
        """
        SELECT i.item_id, c.name AS campaign, c.due_at, i.reviewer_email,
               g.user_email, g.group_name, g.classification_code, g.data_product_name,
               g.valid_to, g.last_recertified_at, g.request_ref
        FROM recert_item i
        JOIN recert_campaign c ON c.campaign_id = i.campaign_id
        JOIN v_grant g ON g.grant_id = i.grant_id
        WHERE i.decision = 'PENDING'
          AND (i.reviewer_email = :e OR EXISTS (
                SELECT 1 FROM delegation d WHERE d.delegator_email = i.reviewer_email AND d.delegate_email = :e
                  AND current_timestamp() BETWEEN d.valid_from AND d.valid_to))
        ORDER BY c.due_at, g.group_name
        """, {"e": email})


def delegations_for(email: str) -> list[dict]:
    return fetch_all("SELECT delegation_id, delegator_email AS delegator, delegate_email AS delegate, valid_from, "
                     "valid_to, reason FROM delegation WHERE (delegator_email = :e OR delegate_email = :e) "
                     "AND valid_to > current_timestamp() ORDER BY valid_from", {"e": email})


# ---- catalog admin ----------------------------------------------------------------
def workspaces() -> list[dict]:
    return fetch_all("SELECT * FROM workspace ORDER BY workspace_seq")


def data_products() -> list[dict]:
    return fetch_all("SELECT d.*, w.name AS workspace_name FROM data_product d "
                     "LEFT JOIN workspace w ON w.workspace_code = d.home_workspace_code ORDER BY d.data_product_seq")


def groups() -> list[dict]:
    return fetch_all("SELECT * FROM v_group ORDER BY group_seq")


def group_members(group_code: str) -> list[dict]:
    return fetch_all("SELECT * FROM v_grant WHERE group_code = :g AND status IN (:live) ORDER BY user_email",
                     {"g": group_code, "live": LIVE})


# ---- fulfilment (manual changes by admins) / audit -----------------------------
def to_grant() -> list[dict]:
    """Approved access an admin still has to add by hand; emergency access first."""
    return fetch_all("SELECT * FROM v_grant WHERE status = 'PENDING_PROVISION' "
                     "ORDER BY is_break_glass DESC, created_at")


def to_remove() -> list[dict]:
    """Access an admin still has to remove by hand: being revoked, or past its end date."""
    return fetch_all("SELECT *, CASE WHEN status = 'ACTIVE' THEN 'EXPIRED' ELSE revoke_reason END AS why "
                     "FROM v_grant WHERE status = 'PENDING_REVOKE' "
                     "OR (status = 'ACTIVE' AND valid_to <= current_timestamp()) "
                     "ORDER BY is_break_glass DESC, coalesce(revoke_requested_at, valid_to)")


def recent_manual_changes(days: int = 30) -> list[dict]:
    return fetch_all(
        "SELECT request_ref, user_email, group_name, data_product_name, 'GRANTED' AS change, "
        "provisioned_at AS changed_at, provisioned_by AS changed_by, fulfilment_note AS note FROM v_grant "
        f"WHERE provisioned_at > current_timestamp() - INTERVAL {int(days)} DAYS "
        "UNION ALL "
        "SELECT request_ref, user_email, group_name, data_product_name, 'REMOVED', revoked_at, removed_by, "
        f"removal_note FROM v_grant WHERE removed_by IS NOT NULL "
        f"AND revoked_at > current_timestamp() - INTERVAL {int(days)} DAYS ORDER BY changed_at DESC")


def failed_notifications() -> list[dict]:
    return fetch_all("SELECT notification_id, channel, subject, attempts, last_error, created_at "
                     "FROM notification_outbox WHERE status IN ('FAILED', 'DEAD') ORDER BY created_at DESC LIMIT 200")


def audit_events(since: datetime, until: datetime, actor: str | None, text: str | None, limit: int = 2000) -> list[dict]:
    sql = ("SELECT chain_seq, occurred_at, actor, actor_type, action, object_kind, object_id, correlation_id, "
           "reason, before_state, after_state, event_hash FROM audit_event WHERE occurred_at BETWEEN :since AND :until")
    params: dict = {"since": since, "until": until}
    if actor:
        sql += " AND actor ILIKE :actor"
        params["actor"] = f"%{actor}%"
    if text:
        sql += " AND (object_id ILIKE :text OR correlation_id ILIKE :text OR action ILIKE :text)"
        params["text"] = f"%{text}%"
    return fetch_all(sql + f" ORDER BY chain_seq DESC LIMIT {int(limit)}", params)


def access_evidence() -> list[dict]:
    """Every live grant with its approval trail - recertification evidence pack (FR-51)."""
    return fetch_all(
        """
        SELECT g.user_email, g.group_name, g.classification_code, g.data_product_code, g.data_product_name,
               g.data_product_type, g.status, g.is_break_glass, g.valid_from, g.valid_to, g.last_recertified_at,
               g.request_ref, a.approvals, g.provisioned_at AS granted_at, g.provisioned_by AS granted_by
        FROM v_grant g
        LEFT JOIN (
          SELECT request_id,
                 array_join(transform(array_sort(collect_list(named_struct('s', stage_no, 't',
                   concat(approver_role, ':', coalesce(decided_by_email, '-'), '@',
                          coalesce(date_format(decided_at, 'yyyy-MM-dd'), '-'))))), x -> x.t), '; ') AS approvals
          FROM approval_task GROUP BY request_id) a ON a.request_id = g.request_id
        WHERE g.status IN (:live) ORDER BY g.group_name, g.user_email
        """, {"live": LIVE})


def owned_data_products(email: str) -> list[dict]:
    """Data products this person is the data owner or steward of."""
    return fetch_all("SELECT data_product_code, name, data_product_type, project_name, catalog_name, schema_name, "
                     "classification_code, "
                     "CASE WHEN owner_email = :e THEN 'DATA_OWNER' ELSE 'STEWARD' END AS role "
                     "FROM data_product WHERE owner_email = :e OR steward_email = :e ORDER BY data_product_seq",
                     {"e": email})


def grants_for_owned(email: str) -> list[dict]:
    """Live grants on the data products this person owns or stewards."""
    return fetch_all("SELECT * FROM v_grant WHERE status IN (:live) AND (owner_email = :e OR steward_email = :e) "
                     "ORDER BY data_product_name, user_email", {"live": LIVE, "e": email})
