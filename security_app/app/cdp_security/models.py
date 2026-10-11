"""Enumerations and small value objects shared across the app and jobs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class _StrEnum(str, Enum):
    def __str__(self) -> str:  # so f-strings and SQL params get the plain value
        return self.value


class RequestType(_StrEnum):
    GRANT = "GRANT"
    EXTEND = "EXTEND"


class RequestStatus(_StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"          # approved; an admin has still to add the member
    FULFILLED = "FULFILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class TaskStatus(_StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class GrantStatus(_StrEnum):
    PENDING_PROVISION = "PENDING_PROVISION"  # an admin adds the member by hand, then marks it granted
    ACTIVE = "ACTIVE"
    PENDING_REVOKE = "PENDING_REVOKE"        # an admin removes the member by hand, then marks it removed
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class RevokeReason(_StrEnum):
    EXPIRED = "EXPIRED"
    SELF = "SELF"
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    RECERT_REVOKE = "RECERT_REVOKE"
    RECERT_NOT_ATTESTED = "RECERT_NOT_ATTESTED"
    LEAVER = "LEAVER"
    GROUP_RETIRED = "GROUP_RETIRED"


class ApproverRole(_StrEnum):
    APPROVER = "APPROVER"    # the data product's owner / approver or approver 2


class AppRole(_StrEnum):
    """Global roles, from Entra group membership. Approvers are per data product."""
    ADMIN = "ADMIN"


@dataclass(frozen=True)
class CurrentUser:
    """The authenticated caller (a person in the App, or a job identity)."""
    email: str
    display_name: str | None = None
    app_roles: frozenset[AppRole] = field(default_factory=frozenset)
    actor_type: str = "USER"           # USER | SERVICE_PRINCIPAL | SYSTEM

    def has(self, role: AppRole) -> bool:
        return role in self.app_roles

    @staticmethod
    def system(job_name: str) -> "CurrentUser":
        return CurrentUser(email=f"system:{job_name}", display_name=job_name, actor_type="SYSTEM")
