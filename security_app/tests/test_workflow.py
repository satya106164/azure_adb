from datetime import date, datetime, timedelta, timezone

import pytest

from cdp_security import workflow as wf
from cdp_security.models import ApproverRole, AppRole, GrantStatus, RequestStatus, RequestType

RULES = [
    wf.ApprovalRule(1, "GENERAL", ApproverRole.DATA_OWNER, 72),
    wf.ApprovalRule(1, "CONFIDENTIAL", ApproverRole.DATA_OWNER, 72),
    wf.ApprovalRule(2, "CONFIDENTIAL", ApproverRole.STEWARD, 72),
    wf.ApprovalRule(3, "RESTRICTED", ApproverRole.SECURITY, 48),
    wf.ApprovalRule(1, "RESTRICTED", ApproverRole.DATA_OWNER, 48),
    wf.ApprovalRule(2, "RESTRICTED", ApproverRole.STEWARD, 48),
]


def ctx(**kw):
    base = dict(requester="req@x.com", beneficiary="ben@x.com",
                role_holders={ApproverRole.DATA_OWNER: {"owner@x.com"}, ApproverRole.STEWARD: {"steward@x.com"}},
                delegations={}, prior_approvers=set())
    base.update(kw)
    return wf.ApprovalContext(**base)


# ---- state machines -----------------------------------------------------------
def test_happy_path_request_transitions():
    s = wf.next_request_status(RequestStatus.DRAFT, "submit", "requester")
    s = wf.next_request_status(s, "approve_stage", "approver")
    s = wf.next_request_status(s, "approve_final", "approver")
    s = wf.next_request_status(s, "start_provisioning", "system")
    assert wf.next_request_status(s, "provisioned", "system") == RequestStatus.FULFILLED


def test_requester_cannot_approve_event():
    with pytest.raises(wf.TransitionError):
        wf.next_request_status(RequestStatus.PENDING_APPROVAL, "approve_final", "requester")


def test_cannot_cancel_after_approval():
    with pytest.raises(wf.TransitionError):
        wf.next_request_status(RequestStatus.APPROVED, "cancel", "admin")


def test_terminal_states_have_no_exits():
    for state in wf.TERMINAL_REQUEST_STATES:
        assert not [k for k in wf.REQUEST_TRANSITIONS if k[0] == state]


def test_grant_removal_outcome_depends_on_reason():
    assert wf.next_grant_status(GrantStatus.PENDING_REVOKE, "removed", "EXPIRED") == GrantStatus.EXPIRED
    assert wf.next_grant_status(GrantStatus.PENDING_REVOKE, "removed", "LEAVER") == GrantStatus.REVOKED
    with pytest.raises(wf.TransitionError):
        wf.next_grant_status(GrantStatus.REVOKED, "revoke")


# ---- routing -------------------------------------------------------------------
def test_plan_stages_ordered_by_stage():
    stages = wf.plan_stages(RULES, "RESTRICTED", RequestType.GRANT)
    assert [s.approver_role for s in stages] == [ApproverRole.DATA_OWNER, ApproverRole.STEWARD, ApproverRole.SECURITY]


def test_plan_stages_fails_closed_without_rules():
    with pytest.raises(wf.ConfigurationError):
        wf.plan_stages(RULES, "UNKNOWN", RequestType.GRANT)


def test_plan_stages_rejects_gaps():
    with pytest.raises(wf.ConfigurationError):
        wf.plan_stages([wf.ApprovalRule(2, "GENERAL", ApproverRole.DATA_OWNER, 1)], "GENERAL", "GRANT")


def test_break_glass_has_no_up_front_stages():
    assert wf.plan_stages(RULES, "RESTRICTED", RequestType.BREAK_GLASS) == []


# ---- segregation of duties ---------------------------------------------------------
def test_owner_can_approve():
    ok, _, behalf = wf.can_decide("owner@x.com", [], ApproverRole.DATA_OWNER, ctx())
    assert ok and behalf is None


def test_requester_who_is_owner_cannot_approve_own_request():
    c = ctx(requester="owner@x.com")
    ok, reason, _ = wf.can_decide("owner@x.com", [], ApproverRole.DATA_OWNER, c)
    assert not ok and "raised" in reason


def test_beneficiary_cannot_approve():
    c = ctx(beneficiary="steward@x.com")
    assert not wf.can_decide("steward@x.com", [], ApproverRole.STEWARD, c)[0]


def test_same_person_cannot_approve_two_stages():
    c = ctx(role_holders={ApproverRole.DATA_OWNER: {"a@x.com"}, ApproverRole.STEWARD: {"a@x.com"}},
            prior_approvers={"a@x.com"})
    assert not wf.can_decide("a@x.com", [], ApproverRole.STEWARD, c)[0]


def test_delegate_acts_on_behalf():
    c = ctx(delegations={"owner@x.com": {"cover@x.com"}})
    ok, _, behalf = wf.can_decide("Cover@X.com", [], ApproverRole.DATA_OWNER, c)
    assert ok and behalf == "owner@x.com"


def test_delegation_from_conflicted_person_does_not_pass_authority():
    c = ctx(requester="owner@x.com", delegations={"owner@x.com": {"cover@x.com"}})
    assert not wf.can_decide("cover@x.com", [], ApproverRole.DATA_OWNER, c)[0]


def test_security_stage_needs_security_role():
    assert not wf.can_decide("sec@x.com", [], ApproverRole.SECURITY, ctx())[0]
    assert wf.can_decide("sec@x.com", [AppRole.SECURITY], ApproverRole.SECURITY, ctx())[0]


def test_admin_role_alone_does_not_approve():
    assert not wf.can_decide("admin@x.com", [AppRole.ADMIN], ApproverRole.DATA_OWNER, ctx())[0]


# ---- time rules ------------------------------------------------------------------
def test_business_hours_skip_weekend():
    friday = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)  # 10:00 BST
    due = wf.add_business_hours(friday, 24)
    assert due == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)  # Monday same time


def test_business_hours_skip_holiday():
    thu = datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc)
    due = wf.add_business_hours(thu, 24, holidays=frozenset({date(2026, 12, 25), date(2026, 12, 28)}))
    assert due.date() == date(2026, 12, 29)


def test_duration_capped_by_classification_and_project():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert wf.effective_valid_to(start, 500, 90) == start + timedelta(days=90)
    end = wf.effective_valid_to(start, 90, 365, project_end=date(2026, 10, 31))
    assert end.date() == date(2026, 10, 31)


def test_duration_rejects_ended_project():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    with pytest.raises(wf.TransitionError):
        wf.effective_valid_to(start, 30, 90, project_end=date(2026, 9, 1))


def test_break_glass_duration_is_hours():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert wf.effective_valid_to(start, 365, 365, request_type="BREAK_GLASS", break_glass_hours=4) == \
        start + timedelta(hours=4)


def test_recert_due_window():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert wf.recert_due(now - timedelta(days=80), 90, 14, now)
    assert not wf.recert_due(now - timedelta(days=70), 90, 14, now)


def test_reviewer_never_the_grantee():
    assert wf.choose_reviewer("owner@x.com", {"owner@x.com"}, {"steward@x.com"}, None) == "steward@x.com"
    assert wf.choose_reviewer("a@x.com", set(), set(), "a@x.com") is None
