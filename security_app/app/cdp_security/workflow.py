"""Workflow rules with no I/O, so they can be unit-tested and reused by app and jobs.

Covers: request and grant state machines, approval stage planning, approver
eligibility (incl. delegation and segregation of duties), SLA due dates in UK
business time, grant duration capping and recertification reviewer selection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .models import (
    ApproverRole, AppRole, GrantStatus, RequestStatus, RequestType, RevokeReason,
)

RS = RequestStatus
GS = GrantStatus


class TransitionError(Exception):
    """An event is not allowed from the current state, or not by this actor."""


class ConfigurationError(Exception):
    """Reference data is incomplete; the workflow fails closed."""


# ---------------------------------------------------------------------------
# Request state machine
# ---------------------------------------------------------------------------
# (from_status, event) -> to_status
REQUEST_TRANSITIONS: dict[tuple[RequestStatus, str], RequestStatus] = {
    (RS.DRAFT, "submit"): RS.PENDING_APPROVAL,
    (RS.DRAFT, "cancel"): RS.CANCELLED,
    (RS.DRAFT, "break_glass"): RS.APPROVED,
    (RS.PENDING_APPROVAL, "approve_stage"): RS.PENDING_APPROVAL,
    (RS.PENDING_APPROVAL, "approve_final"): RS.APPROVED,
    (RS.PENDING_APPROVAL, "reject"): RS.REJECTED,
    (RS.PENDING_APPROVAL, "timeout"): RS.REJECTED,
    (RS.PENDING_APPROVAL, "cancel"): RS.CANCELLED,
    (RS.APPROVED, "extended"): RS.FULFILLED,          # EXTEND requests need no change to the group
    (RS.APPROVED, "granted"): RS.FULFILLED,           # an admin added the member by hand
    (RS.APPROVED, "withdraw"): RS.CANCELLED,          # group retired / person left before it was done
}

# Who may raise each event
REQUEST_EVENT_ACTORS: dict[str, frozenset[str]] = {
    "submit": frozenset({"requester"}),
    "break_glass": frozenset({"break_glass"}),
    "cancel": frozenset({"requester", "admin", "system"}),  # system: request became pointless
    "approve_stage": frozenset({"approver"}),
    "approve_final": frozenset({"approver"}),
    "reject": frozenset({"approver"}),
    "timeout": frozenset({"system"}),
    "extended": frozenset({"system", "approver"}),
    "granted": frozenset({"admin"}),
    "withdraw": frozenset({"admin", "system"}),
}

TERMINAL_REQUEST_STATES = frozenset({RS.FULFILLED, RS.REJECTED, RS.CANCELLED})


def next_request_status(current: RequestStatus | str, event: str, actor_kind: str) -> RequestStatus:
    current = RS(current)
    allowed = REQUEST_EVENT_ACTORS.get(event)
    if allowed is None:
        raise TransitionError(f"Unknown event {event!r}")
    if actor_kind not in allowed:
        raise TransitionError(f"{actor_kind!r} may not raise {event!r}")
    try:
        return REQUEST_TRANSITIONS[(current, event)]
    except KeyError:
        raise TransitionError(f"Cannot {event} a request that is {current.value}") from None


# ---------------------------------------------------------------------------
# Grant state machine
# ---------------------------------------------------------------------------
# The app never changes a group itself: an admin does it by hand and marks it done ("granted", "removed").
GRANT_TRANSITIONS: dict[tuple[GrantStatus, str], GrantStatus] = {
    (GS.PENDING_PROVISION, "granted"): GS.ACTIVE,
    (GS.PENDING_PROVISION, "withdraw"): GS.REVOKED,   # never added, so nothing to remove
    (GS.ACTIVE, "extend"): GS.ACTIVE,
    (GS.ACTIVE, "recertify"): GS.ACTIVE,
    (GS.ACTIVE, "revoke"): GS.PENDING_REVOKE,
}


def next_grant_status(current: GrantStatus | str, event: str,
                      revoke_reason: RevokeReason | str | None = None) -> GrantStatus:
    current = GS(current)
    if (current, event) == (GS.PENDING_REVOKE, "removed"):
        # Removal outcome depends on why it was revoked
        return GS.EXPIRED if revoke_reason and RevokeReason(revoke_reason) == RevokeReason.EXPIRED else GS.REVOKED
    try:
        return GRANT_TRANSITIONS[(current, event)]
    except KeyError:
        raise TransitionError(f"Cannot {event} a grant that is {current.value}") from None


# ---------------------------------------------------------------------------
# Approval routing
# ---------------------------------------------------------------------------
@dataclass(frozen=True, order=True)
class ApprovalRule:
    stage_no: int
    classification: str
    approver_role: ApproverRole
    sla_hours: int


def plan_stages(rules: Iterable[ApprovalRule], classification: str,
                request_type: RequestType | str) -> list[ApprovalRule]:
    """Ordered approval stages for a request. Fails closed if none are configured."""
    request_type = RequestType(request_type)
    if request_type == RequestType.BREAK_GLASS:
        return []  # approved immediately; reviewed post hoc by SECURITY
    stages = sorted(r for r in rules if r.classification == classification)
    if not stages:
        raise ConfigurationError(f"No approval rules configured for classification {classification}")
    numbers = [s.stage_no for s in stages]
    if numbers != list(range(1, len(numbers) + 1)):
        raise ConfigurationError(f"Approval stages for {classification} must be 1..n without gaps, got {numbers}")
    return stages


def _norm(email: str | None) -> str:
    return (email or "").strip().lower()


@dataclass
class ApprovalContext:
    """Everything needed to decide who may approve a given request."""
    requester: str
    beneficiary: str
    # Named approvers per role (SECURITY is satisfied by app role instead).
    role_holders: dict[ApproverRole, set[str]] = field(default_factory=dict)
    # delegator email -> set of delegate emails, active now
    delegations: dict[str, set[str]] = field(default_factory=dict)
    # people who already approved an earlier stage of this request
    prior_approvers: set[str] = field(default_factory=set)
    distinct_approvers: bool = True

    def excluded(self) -> set[str]:
        out = {_norm(self.requester), _norm(self.beneficiary)}
        if self.distinct_approvers:
            out |= {_norm(e) for e in self.prior_approvers}
        return out


def eligible_approvers(role: ApproverRole, ctx: ApprovalContext) -> dict[str, str | None]:
    """Map of eligible approver email -> the person they act on behalf of (None if themselves).

    Segregation of duties: requester, beneficiary and (by default) anyone who
    approved an earlier stage are excluded, and so are their delegates acting
    for them - authority cannot be passed around a conflict.
    """
    excluded = ctx.excluded()
    holders = {_norm(e) for e in ctx.role_holders.get(ApproverRole(role), set())}
    out: dict[str, str | None] = {}
    for holder in holders:
        if holder in excluded:
            continue
        out.setdefault(holder, None)
        for delegate in ctx.delegations.get(holder, set()):
            d = _norm(delegate)
            if d not in excluded:
                out.setdefault(d, holder)
    return out


def can_decide(actor_email: str, actor_roles: Iterable[AppRole], stage_role: ApproverRole | str,
               ctx: ApprovalContext) -> tuple[bool, str, str | None]:
    """Return (allowed, reason, on_behalf_of_email)."""
    actor = _norm(actor_email)
    stage_role = ApproverRole(stage_role)
    if actor in {_norm(ctx.requester), _norm(ctx.beneficiary)}:
        return False, "You cannot approve a request you raised or that grants access to you.", None
    if ctx.distinct_approvers and actor in {_norm(e) for e in ctx.prior_approvers}:
        return False, "You already approved an earlier stage; a different person must approve this stage.", None
    if stage_role == ApproverRole.SECURITY:
        if AppRole.SECURITY in set(actor_roles):
            return True, "Security approver", None
        return False, "This stage needs a member of the security approvers group.", None
    eligible = eligible_approvers(stage_role, ctx)
    if actor in eligible:
        return True, "Eligible approver", eligible[actor]
    return False, f"You are not a {stage_role.value.replace('_', ' ').lower()} (or delegate) for this access.", None


# ---------------------------------------------------------------------------
# Time rules
# ---------------------------------------------------------------------------
def add_business_hours(start: datetime, hours: int, holidays: frozenset[date] = frozenset(),
                       tz: str = "Europe/London") -> datetime:
    """Add `hours`, counting only weekdays that are not holidays (local calendar).

    A business day counts as 24 hours, so 72 business hours = 3 working days.
    """
    if start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    zone = ZoneInfo(tz)
    cur = start.astimezone(zone)
    remaining = timedelta(hours=hours)
    while remaining > timedelta(0):
        next_midnight = datetime.combine(cur.date() + timedelta(days=1), time.min, tzinfo=zone)
        if cur.weekday() >= 5 or cur.date() in holidays:
            cur = next_midnight
            continue
        step = min(remaining, next_midnight - cur)
        cur += step
        remaining -= step
    return cur.astimezone(timezone.utc)


def effective_valid_to(start: datetime, requested_days: int | None, default_days: int,
                       request_type: RequestType | str = RequestType.GRANT, break_glass_hours: int = 4) -> datetime:
    """Grant end: the requested number of days (no upper limit; default_days if none was given).
    Break-glass is hours, not days."""
    if RequestType(request_type) == RequestType.BREAK_GLASS:
        return start + timedelta(hours=break_glass_hours)
    days = int(requested_days) if requested_days else default_days
    return start + timedelta(days=max(days, 1))


def recert_due(last_attested: datetime, interval_days: int, window_days: int, now: datetime) -> bool:
    """True when a grant should be put into a recertification campaign."""
    return last_attested + timedelta(days=interval_days) - timedelta(days=window_days) <= now


def choose_reviewer(grantee: str, owners: Iterable[str], stewards: Iterable[str]) -> str | None:
    """Pick a recertification reviewer who is not the grantee (data owner, then steward)."""
    g = _norm(grantee)
    for pool in (sorted(owners), sorted(stewards)):
        for candidate in pool:
            if _norm(candidate) and _norm(candidate) != g:
                return _norm(candidate)
    return None
