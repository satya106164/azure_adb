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
    BREAK_GLASS = "BREAK_GLASS"


class RequestStatus(_StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PROVISIONING = "PROVISIONING"
    FULFILLED = "FULFILLED"
    PROVISIONING_FAILED = "PROVISIONING_FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class TaskStatus(_StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class GrantStatus(_StrEnum):
    PENDING_PROVISION = "PENDING_PROVISION"
    ACTIVE = "ACTIVE"
    PENDING_REVOKE = "PENDING_REVOKE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class RevokeReason(_StrEnum):
    EXPIRED = "EXPIRED"
    SELF = "SELF"
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    RECERT_REVOKE = "RECERT_REVOKE"
    RECERT_NOT_ATTESTED = "RECERT_NOT_ATTESTED"
    LEAVER = "LEAVER"
    POST_HOC_REJECTED = "POST_HOC_REJECTED"
    GROUP_RETIRED = "GROUP_RETIRED"


class ApproverRole(_StrEnum):
    DATA_OWNER = "DATA_OWNER"
    STEWARD = "STEWARD"
    SECURITY = "SECURITY"
    PROJECT_SPONSOR = "PROJECT_SPONSOR"


class AppRole(_StrEnum):
    """Global roles. Data owner / steward / sponsor are contextual (per object)."""
    ADMIN = "ADMIN"
    SECURITY = "SECURITY"
    AUDITOR = "AUDITOR"
    BREAK_GLASS = "BREAK_GLASS"


class GroupPurpose(_StrEnum):
    DATA_PRODUCT = "DATA_PRODUCT"
    PROJECT = "PROJECT"
    AI_USE_CASE = "AI_USE_CASE"
    WORKSPACE = "WORKSPACE"


class PrivilegeLevel(_StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    CONTRIB = "CONTRIB"
    USER = "USER"
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
