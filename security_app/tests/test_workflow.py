from datetime import date, datetime, timedelta, timezone

import pytest

from cdp_security import workflow as wf
from cdp_security.models import ApproverRole, AppRole, GrantStatus, RequestStatus, RequestType, RevokeReason

APPROVER = ApproverRole.APPROVER


def ctx(**kw):
    base = dict(requester="req@x.com", beneficiary="ben@x.com",
                role_holders={APPROVER: {"owner@x.com", "approver2@x.com"}},
                delegations={}, prior_approvers=set())
    base.update(kw)
    return wf.ApprovalContext(**base)


# ---- state machines -----------------------------------------------------------
def test_happy_path_request_transitions():
    s = wf.next_request_status(RequestStatus.DRAFT, "submit", "requester")
    s = wf.next_request_status(s, "approve_stage", "approver")
    s = wf.next_request_status(s, "approve_final", "approver")
    assert wf.next_request_status(s, "granted", "admin") == RequestStatus.FULFILLED


def test_only_an_admin_records_a_manual_grant():
    for actor in ("system", "approver", "requester"):
        with pytest.raises(wf.TransitionError):
            wf.next_request_status(RequestStatus.APPROVED, "granted", actor)


def test_requester_cannot_approve_event():
    with pytest.raises(wf.TransitionError):
        wf.next_request_status(RequestStatus.PENDING_APPROVAL, "approve_final", "requester")


def test_cannot_cancel_after_approval():
    with pytest.raises(wf.TransitionError):
        wf.next_request_status(RequestStatus.APPROVED, "cancel", "admin")


def test_terminal_states_have_no_exits():
    for state in wf.TERMINAL_REQUEST_STATES:
        assert not [k for k in wf.REQUEST_TRANSITIONS if k[0] == state]


def test_grant_is_live_only_once_an_admin_marks_it():
    assert wf.next_grant_status(GrantStatus.PENDING_PROVISION, "granted") == GrantStatus.ACTIVE
    assert wf.next_grant_status(GrantStatus.PENDING_PROVISION, "withdraw") == GrantStatus.REVOKED
    with pytest.raises(wf.TransitionError):
        wf.next_grant_status(GrantStatus.ACTIVE, "granted")


def test_grant_removal_outcome_depends_on_reason():
    assert wf.next_grant_status(GrantStatus.PENDING_REVOKE, "removed", "EXPIRED") == GrantStatus.EXPIRED
    assert wf.next_grant_status(GrantStatus.PENDING_REVOKE, "removed", "LEAVER") == GrantStatus.REVOKED
    with pytest.raises(wf.TransitionError):
        wf.next_grant_status(GrantStatus.REVOKED, "revoke")


# ---- routing -------------------------------------------------------------------
def test_one_approver_stage_for_every_request():
    assert wf.plan_stages(72) == [wf.ApprovalRule(1, APPROVER, 72)]


def test_break_glass_is_gone():
    assert "BREAK_GLASS" not in RequestType.__members__
    assert list(AppRole.__members__) == ["ADMIN"]  # admins run the app; no break-glass, security or auditor
    assert not [k for k in wf.REQUEST_TRANSITIONS if k[1] == "break_glass"]
    assert "POST_HOC_REJECTED" not in RevokeReason.__members__


# ---- segregation of duties ---------------------------------------------------------
def test_either_approver_can_approve():
    for who in ("owner@x.com", "approver2@x.com"):
        ok, _, behalf = wf.can_decide(who, [], APPROVER, ctx())
        assert ok and behalf is None
    assert not wf.can_decide("someone@x.com", [], APPROVER, ctx())[0]


def test_approver_2_decides_when_the_owner_raised_the_request():
    c = ctx(requester="owner@x.com")
    ok, reason, _ = wf.can_decide("owner@x.com", [], APPROVER, c)
    assert not ok and "raised" in reason
    assert wf.can_decide("approver2@x.com", [], APPROVER, c)[0]
    assert set(wf.eligible_approvers(APPROVER, c)) == {"approver2@x.com"}


def test_beneficiary_cannot_approve():
    c = ctx(beneficiary="approver2@x.com")
    assert not wf.can_decide("approver2@x.com", [], APPROVER, c)[0]


def test_delegate_acts_on_behalf():
    c = ctx(delegations={"owner@x.com": {"cover@x.com"}})
    ok, _, behalf = wf.can_decide("Cover@X.com", [], APPROVER, c)
    assert ok and behalf == "owner@x.com"


def test_delegation_from_conflicted_person_does_not_pass_authority():
    c = ctx(requester="owner@x.com", delegations={"owner@x.com": {"cover@x.com"}})
    assert not wf.can_decide("cover@x.com", [], APPROVER, c)[0]


def test_admin_role_alone_does_not_approve():
    assert not wf.can_decide("admin@x.com", [AppRole.ADMIN], APPROVER, ctx())[0]


def test_admin_can_decide_on_the_approvers_behalf():
    assert wf.can_override("admin@x.com", [AppRole.ADMIN], ctx())[0]
    assert not wf.can_override("someone@x.com", [], ctx())[0]


def test_admin_override_never_for_own_request():
    assert not wf.can_override("admin@x.com", [AppRole.ADMIN], ctx(requester="admin@x.com"))[0]
    assert not wf.can_override("admin@x.com", [AppRole.ADMIN], ctx(beneficiary="admin@x.com"))[0]


# ---- time rules ------------------------------------------------------------------
def test_business_hours_skip_weekend():
    friday = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)  # 10:00 BST
    due = wf.add_business_hours(friday, 24)
    assert due == datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)  # Monday same time


def test_business_hours_skip_holiday():
    thu = datetime(2026, 12, 24, 12, 0, tzinfo=timezone.utc)
    due = wf.add_business_hours(thu, 24, holidays=frozenset({date(2026, 12, 25), date(2026, 12, 28)}))
    assert due.date() == date(2026, 12, 29)


def test_duration_is_what_was_requested():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert wf.effective_valid_to(start, 1185, 90) == start + timedelta(days=1185)  # no upper limit
    assert wf.effective_valid_to(start, None, 180) == start + timedelta(days=180)
    assert wf.effective_valid_to(start, 30, 365) == start + timedelta(days=30)


def test_recert_due_window():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert wf.recert_due(now - timedelta(days=80), 90, 14, now)
    assert not wf.recert_due(now - timedelta(days=70), 90, 14, now)


def test_reviewer_is_the_owner_then_approver_2_never_the_grantee():
    assert wf.choose_reviewer("ben@x.com", ["owner@x.com", "approver2@x.com"]) == "owner@x.com"
    assert wf.choose_reviewer("owner@x.com", ["owner@x.com", "approver2@x.com"]) == "approver2@x.com"
    assert wf.choose_reviewer("a@x.com", ["a@x.com"]) is None


def test_system_can_close_a_pending_request():
    assert wf.next_request_status(RequestStatus.PENDING_APPROVAL, "cancel", "system") == RequestStatus.CANCELLED
