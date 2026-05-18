from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from secrets import token_urlsafe
from typing import Any

DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 60


class LeaseStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


INITIAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.BLOCKED,
    }
)
TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)
TASK_STATUS_TRANSITIONS = {
    TaskStatus.QUEUED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.BLOCKED,
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class CredentialPolicy:
    allowed_agents: frozenset[str]
    allowed_actions: frozenset[str]
    approval_required_actions: frozenset[str] = field(default_factory=frozenset)
    max_ttl_seconds: int = 300
    require_intent: bool = True
    agent_lease_limit: int | None = None
    credential_lease_limit: int | None = None
    credential_action_limit: int | None = None
    rate_limit_window_seconds: int = DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    provider_token_budget: int | None = None
    provider_cost_budget_cents: int | None = None


@dataclass(frozen=True)
class CredentialRecord:
    id: str
    name: str
    provider: str
    secret_ref: str
    policy: CredentialPolicy
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "policy": {
                "allowed_agents": sorted(self.policy.allowed_agents),
                "allowed_actions": sorted(self.policy.allowed_actions),
                "approval_required_actions": sorted(self.policy.approval_required_actions),
                "max_ttl_seconds": self.policy.max_ttl_seconds,
                "require_intent": self.policy.require_intent,
                "agent_lease_limit": self.policy.agent_lease_limit,
                "credential_lease_limit": self.policy.credential_lease_limit,
                "credential_action_limit": self.policy.credential_action_limit,
                "rate_limit_window_seconds": self.policy.rate_limit_window_seconds,
                "provider_token_budget": self.policy.provider_token_budget,
                "provider_cost_budget_cents": self.policy.provider_cost_budget_cents,
            },
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class IntentReview:
    allowed: bool
    reason: str
    normalized_action: str


@dataclass(frozen=True)
class CredentialLease:
    id: str
    credential_id: str
    agent_id: str
    action: str
    intent: str
    issued_at: datetime
    expires_at: datetime
    ttl_seconds: int
    token: str
    status: LeaseStatus = LeaseStatus.ACTIVE

    @classmethod
    def issue(
        cls,
        *,
        credential_id: str,
        agent_id: str,
        action: str,
        intent: str,
        ttl_seconds: int,
    ) -> "CredentialLease":
        return cls._create(
            credential_id=credential_id,
            agent_id=agent_id,
            action=action,
            intent=intent,
            ttl_seconds=ttl_seconds,
            status=LeaseStatus.ACTIVE,
        )

    @classmethod
    def request_approval(
        cls,
        *,
        credential_id: str,
        agent_id: str,
        action: str,
        intent: str,
        ttl_seconds: int,
    ) -> "CredentialLease":
        return cls._create(
            credential_id=credential_id,
            agent_id=agent_id,
            action=action,
            intent=intent,
            ttl_seconds=ttl_seconds,
            status=LeaseStatus.PENDING,
            token=f"hvp_{token_urlsafe(24)}",
        )

    @classmethod
    def _create(
        cls,
        *,
        credential_id: str,
        agent_id: str,
        action: str,
        intent: str,
        ttl_seconds: int,
        status: LeaseStatus,
        token: str | None = None,
    ) -> "CredentialLease":
        issued_at = datetime.now(timezone.utc)
        return cls(
            id=f"lease_{token_urlsafe(12)}",
            credential_id=credential_id,
            agent_id=agent_id,
            action=action,
            intent=intent,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
            ttl_seconds=ttl_seconds,
            token=token or f"hvl_{token_urlsafe(24)}",
            status=status,
        )

    def is_active(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return self.status == LeaseStatus.ACTIVE and current < self.expires_at

    def activate(self) -> "CredentialLease":
        issued_at = datetime.now(timezone.utc)
        return replace(
            self,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=self.ttl_seconds),
            token=f"hvl_{token_urlsafe(24)}",
            status=LeaseStatus.ACTIVE,
        )

    def deny(self) -> "CredentialLease":
        return replace(self, status=LeaseStatus.DENIED)

    def public_view(self) -> dict[str, Any]:
        status = self.status
        if status == LeaseStatus.ACTIVE and not self.is_active():
            status = LeaseStatus.EXPIRED
        return {
            "id": self.id,
            "credential_id": self.credential_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "intent": self.intent,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ttl_seconds": self.ttl_seconds,
            "status": status,
            "token_preview": f"{self.token[:8]}..." if status not in {LeaseStatus.PENDING, LeaseStatus.DENIED} else "not issued",
        }


@dataclass(frozen=True)
class AuditEvent:
    type: str
    actor_id: str
    target_id: str
    decision: str
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_view(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "actor_id": self.actor_id,
            "target_id": self.target_id,
            "decision": self.decision,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }
