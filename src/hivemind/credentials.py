from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any, cast

from hivemind.models import (
    AuditEvent,
    CredentialLease,
    CredentialPolicy,
    CredentialRecord,
    DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    LeaseStatus,
)
from hivemind.policy import PolicyEngine
from hivemind.secret_refs import validate_external_credential_metadata, validate_external_secret_ref


class CredentialError(ValueError):
    pass


LEASE_DENIED_EVENT = "credential.lease.denied"
LEASE_REQUEST_COUNTED_METADATA_KEY = "lease_request_counted"
LEASE_REQUEST_RATE_LIMIT_EVENTS = frozenset(
    {"credential.lease.issued", "credential.lease.pending", LEASE_DENIED_EVENT}
)
ACTION_DENIED_EVENT = "credential.action.denied"
ACTION_RATE_LIMIT_EVENTS = frozenset({"credential.action.performed"})
LEASE_EXPIRED_OR_REVOKED_REASON = "credential lease is expired or revoked"


class CredentialVault:
    """In-memory secret reference registry.

    The app stores references to secrets, not raw secret values. Container
    operators can map these references to their actual secret backend later.
    """

    def __init__(self) -> None:
        self._credentials: dict[str, CredentialRecord] = {}
        self._lock = RLock()

    def add(
        self,
        *,
        credential_id: str,
        name: str,
        provider: str,
        secret_ref: str,
        policy: CredentialPolicy,
        metadata: dict[str, Any] | None = None,
    ) -> CredentialRecord:
        with self._lock:
            if credential_id in self._credentials:
                raise CredentialError(f"credential already exists: {credential_id}")
            try:
                secret_ref = validate_external_secret_ref(secret_ref)
                validate_external_credential_metadata(metadata or {})
            except ValueError as exc:
                raise CredentialError(str(exc)) from exc
            record = CredentialRecord(
                id=credential_id,
                name=name,
                provider=provider,
                secret_ref=secret_ref,
                policy=policy,
                metadata=metadata or {},
            )
            self._credentials[credential_id] = record
            return record

    def get(self, credential_id: str) -> CredentialRecord:
        with self._lock:
            try:
                return self._credentials[credential_id]
            except KeyError as exc:
                raise CredentialError(f"unknown credential: {credential_id}") from exc

    def list(self) -> list[CredentialRecord]:
        with self._lock:
            return list(self._credentials.values())


class CredentialService:
    def __init__(self, vault: CredentialVault, policy_engine: PolicyEngine | None = None) -> None:
        self._vault = vault
        self._policy_engine = policy_engine or PolicyEngine()
        self._leases: dict[str, CredentialLease] = {}
        self._audit_events: list[AuditEvent] = []
        self._lock = RLock()

    def request_lease(
        self,
        *,
        credential_id: str,
        agent_id: str,
        action: str,
        intent: str,
        ttl_seconds: int | None = None,
    ) -> CredentialLease:
        credential = self._vault.get(credential_id)
        deterministic_review = self._policy_engine.review_deterministic_intent(
            credential=credential,
            agent_id=agent_id,
            action=action,
            intent=intent,
        )
        if not deterministic_review.allowed:
            self._record_audit(
                AuditEvent(
                    type=LEASE_DENIED_EVENT,
                    actor_id=agent_id,
                    target_id=credential_id,
                    decision="denied",
                    reason=deterministic_review.reason,
                    metadata={"action": deterministic_review.normalized_action},
                )
            )
            raise CredentialError(deterministic_review.reason)

        denial_reason = self._record_lease_request_rate_limit_denial_if_limited(
            credential=credential,
            agent_id=agent_id,
            action=deterministic_review.normalized_action,
        )
        if denial_reason is not None:
            raise CredentialError(denial_reason)

        review = self._policy_engine.review_intent(
            credential=credential,
            agent_id=agent_id,
            action=action,
            intent=intent,
        )

        if not review.allowed:
            self._record_audit(
                AuditEvent(
                    type=LEASE_DENIED_EVENT,
                    actor_id=agent_id,
                    target_id=credential_id,
                    decision="denied",
                    reason=review.reason,
                    metadata={
                        "action": review.normalized_action,
                        LEASE_REQUEST_COUNTED_METADATA_KEY: True,
                    },
                )
            )
            raise CredentialError(review.reason)

        requested_ttl = ttl_seconds or credential.policy.max_ttl_seconds
        ttl = min(requested_ttl, credential.policy.max_ttl_seconds)
        requires_approval = review.normalized_action in credential.policy.approval_required_actions
        lease = (
            CredentialLease.request_approval(
                credential_id=credential.id,
                agent_id=agent_id,
                action=review.normalized_action,
                intent=intent,
                ttl_seconds=ttl,
            )
            if requires_approval
            else CredentialLease.issue(
                credential_id=credential.id,
                agent_id=agent_id,
                action=review.normalized_action,
                intent=intent,
                ttl_seconds=ttl,
            )
        )

        with self._lock:
            denial_reason, denial_metadata = self._lease_request_rate_limit_denial(
                credential=credential,
                agent_id=agent_id,
            )
            if denial_reason is not None:
                self._audit_events.append(
                    AuditEvent(
                        type=LEASE_DENIED_EVENT,
                        actor_id=agent_id,
                        target_id=credential_id,
                        decision="denied",
                        reason=denial_reason,
                        metadata={"action": review.normalized_action, **denial_metadata},
                    )
                )
                raise CredentialError(denial_reason)
            self._leases[lease.id] = lease
            self._audit_events.append(
                AuditEvent(
                    type="credential.lease.pending" if requires_approval else "credential.lease.issued",
                    actor_id=agent_id,
                    target_id=credential_id,
                    decision="pending" if requires_approval else "allowed",
                    reason="action requires operator approval" if requires_approval else review.reason,
                    metadata={"action": lease.action, "ttl_seconds": ttl, "lease_id": lease.id},
                )
            )
        return lease

    def perform_action(self, *, lease_token: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_action = action.strip().lower()
        try:
            lease = self._find_lease_by_token(lease_token)
        except CredentialError:
            self._record_audit(
                AuditEvent(
                    type=ACTION_DENIED_EVENT,
                    actor_id="unknown",
                    target_id="credential_lease",
                    decision="denied",
                    reason="unknown credential lease token",
                    metadata={"action": normalized_action},
                )
            )
            raise

        credential = self._vault.get(lease.credential_id)
        self._consume_action_or_record_denial(
            lease=lease,
            credential=credential,
            action=normalized_action,
            payload=payload,
        )
        return {
            "ok": True,
            "provider": credential.provider,
            "credential_id": credential.id,
            "action": normalized_action,
            "result": "credential lease matched requested action",
        }

    def approve_lease(self, *, lease_id: str, approved_by: str) -> CredentialLease:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise CredentialError(f"unknown lease: {lease_id}")
            if lease.status != LeaseStatus.PENDING:
                raise CredentialError("credential lease is not pending approval")
            approved = lease.activate()
            self._leases[lease_id] = approved
        self._record_audit(
            AuditEvent(
                type="credential.lease.approved",
                actor_id=approved_by,
                target_id=approved.credential_id,
                decision="allowed",
                reason="operator approved lease request",
                metadata={
                    "action": approved.action,
                    "agent_id": approved.agent_id,
                    "lease_id": approved.id,
                    "ttl_seconds": approved.ttl_seconds,
                },
            )
        )
        return approved

    def deny_lease(self, *, lease_id: str, denied_by: str) -> CredentialLease:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise CredentialError(f"unknown lease: {lease_id}")
            if lease.status != LeaseStatus.PENDING:
                raise CredentialError("credential lease is not pending approval")
            denied = lease.deny()
            self._leases[lease_id] = denied
        self._record_audit(
            AuditEvent(
                type=LEASE_DENIED_EVENT,
                actor_id=denied_by,
                target_id=denied.credential_id,
                decision="denied",
                reason="operator denied lease request",
                metadata={
                    "action": denied.action,
                    "agent_id": denied.agent_id,
                    "lease_id": denied.id,
                    "ttl_seconds": denied.ttl_seconds,
                },
            )
        )
        return denied

    def revoke_lease(self, lease_id: str) -> CredentialLease:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise CredentialError(f"unknown lease: {lease_id}")
            revoked = cast(CredentialLease, replace(lease, status=LeaseStatus.REVOKED))
            self._leases[lease_id] = revoked
            return revoked

    def list_leases(self) -> list[CredentialLease]:
        with self._lock:
            return list(self._leases.values())

    def audit_events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._audit_events)

    def _find_lease_by_token(self, lease_token: str) -> CredentialLease:
        with self._lock:
            for lease in self._leases.values():
                if lease.token == lease_token:
                    return lease
        raise CredentialError("unknown credential lease token")

    def _lease_request_rate_limit_denial(
        self,
        *,
        credential: CredentialRecord,
        agent_id: str,
    ) -> tuple[str | None, dict[str, Any]]:
        window_seconds = credential.policy.rate_limit_window_seconds or DEFAULT_RATE_LIMIT_WINDOW_SECONDS
        if credential.policy.agent_lease_limit is not None:
            count = self._count_recent_events(
                event_types=LEASE_REQUEST_RATE_LIMIT_EVENTS,
                target_id=credential.id,
                actor_id=agent_id,
                window_seconds=window_seconds,
            )
            if count >= credential.policy.agent_lease_limit:
                return (
                    "agent lease request rate limit exceeded",
                    {
                        "rate_limit": "agent_lease_limit",
                        "limit": credential.policy.agent_lease_limit,
                        "count": count,
                        "window_seconds": window_seconds,
                    },
                )
        if credential.policy.credential_lease_limit is not None:
            count = self._count_recent_events(
                event_types=LEASE_REQUEST_RATE_LIMIT_EVENTS,
                target_id=credential.id,
                actor_id=None,
                window_seconds=window_seconds,
            )
            if count >= credential.policy.credential_lease_limit:
                return (
                    "credential lease request rate limit exceeded",
                    {
                        "rate_limit": "credential_lease_limit",
                        "limit": credential.policy.credential_lease_limit,
                        "count": count,
                        "window_seconds": window_seconds,
                    },
                )
        return None, {}

    def _credential_action_rate_limit_denial(
        self,
        *,
        credential: CredentialRecord,
    ) -> tuple[str | None, dict[str, Any]]:
        with self._lock:
            return self._credential_action_rate_limit_denial_locked(credential=credential)

    def _credential_action_rate_limit_denial_locked(
        self,
        *,
        credential: CredentialRecord,
    ) -> tuple[str | None, dict[str, Any]]:
        if credential.policy.credential_action_limit is None:
            return None, {}
        window_seconds = credential.policy.rate_limit_window_seconds or DEFAULT_RATE_LIMIT_WINDOW_SECONDS
        count = self._count_recent_events_locked(
            event_types=ACTION_RATE_LIMIT_EVENTS,
            target_id=credential.id,
            actor_id=None,
            window_seconds=window_seconds,
        )
        if count < credential.policy.credential_action_limit:
            return None, {}
        return (
            "credential action rate limit exceeded",
            {
                "rate_limit": "credential_action_limit",
                "limit": credential.policy.credential_action_limit,
                "count": count,
                "window_seconds": window_seconds,
            },
        )

    def _count_recent_events(
        self,
        *,
        event_types: frozenset[str],
        target_id: str,
        actor_id: str | None,
        window_seconds: int,
    ) -> int:
        window_start = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        with self._lock:
            return sum(
                1
                for event in self._audit_events
                if self._event_counts_toward_rate_limit(event, event_types)
                and event.target_id == target_id
                and event.created_at >= window_start
                and (actor_id is None or event.actor_id == actor_id)
            )

    def _count_recent_events_locked(
        self,
        *,
        event_types: frozenset[str],
        target_id: str,
        actor_id: str | None,
        window_seconds: int,
    ) -> int:
        window_start = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        return sum(
            1
            for event in self._audit_events
            if self._event_counts_toward_rate_limit(event, event_types)
            and event.target_id == target_id
            and event.created_at >= window_start
            and (actor_id is None or event.actor_id == actor_id)
        )

    def _event_counts_toward_rate_limit(self, event: AuditEvent, event_types: frozenset[str]) -> bool:
        if event.type not in event_types:
            return False
        if event.type != LEASE_DENIED_EVENT:
            return True
        return event.metadata.get(LEASE_REQUEST_COUNTED_METADATA_KEY) is True

    def _record_lease_request_rate_limit_denial_if_limited(
        self,
        *,
        credential: CredentialRecord,
        agent_id: str,
        action: str,
    ) -> str | None:
        with self._lock:
            denial_reason, denial_metadata = self._lease_request_rate_limit_denial(
                credential=credential,
                agent_id=agent_id,
            )
            if denial_reason is None:
                return None
            self._audit_events.append(
                AuditEvent(
                    type=LEASE_DENIED_EVENT,
                    actor_id=agent_id,
                    target_id=credential.id,
                    decision="denied",
                    reason=denial_reason,
                    metadata={"action": action, **denial_metadata},
                )
            )
            return denial_reason

    def _consume_action_or_record_denial(
        self,
        *,
        lease: CredentialLease,
        credential: CredentialRecord,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        with self._lock:
            current = self._leases.get(lease.id)
            if current is None:
                self._audit_events.append(
                    self._action_denied_event(
                        lease=lease,
                        action=action,
                        reason=LEASE_EXPIRED_OR_REVOKED_REASON,
                    )
                )
                raise CredentialError(LEASE_EXPIRED_OR_REVOKED_REASON)
            if current.status == LeaseStatus.PENDING:
                self._audit_events.append(
                    self._action_denied_event(
                        lease=current,
                        action=action,
                        reason="credential lease is pending approval",
                    )
                )
                raise CredentialError("credential lease is pending approval")
            if current.status == LeaseStatus.DENIED:
                self._audit_events.append(
                    self._action_denied_event(
                        lease=current,
                        action=action,
                        reason="credential lease request was denied",
                    )
                )
                raise CredentialError("credential lease request was denied")
            if not current.is_active():
                self._audit_events.append(
                    self._action_denied_event(
                        lease=current,
                        action=action,
                        reason=LEASE_EXPIRED_OR_REVOKED_REASON,
                    )
                )
                raise CredentialError(LEASE_EXPIRED_OR_REVOKED_REASON)
            if current.action != action:
                self._audit_events.append(
                    self._action_denied_event(
                        lease=current,
                        action=action,
                        reason="credential lease does not allow this action",
                    )
                )
                raise CredentialError("credential lease does not allow this action")

            denial_reason, denial_metadata = self._credential_action_rate_limit_denial_locked(credential=credential)
            if denial_reason is not None:
                self._audit_events.append(
                    self._action_denied_event(
                        lease=current,
                        action=action,
                        reason=denial_reason,
                        metadata=denial_metadata,
                    )
                )
                raise CredentialError(denial_reason)

            consumed = replace(current, status=LeaseStatus.REVOKED)
            self._leases[current.id] = consumed
            self._audit_events.append(
                AuditEvent(
                    type="credential.action.performed",
                    actor_id=consumed.agent_id,
                    target_id=credential.id,
                    decision="allowed",
                    reason="action matched active credential lease",
                    metadata={"action": action, "payload_keys": sorted(payload.keys())},
                )
            )

    def _record_action_denied(
        self,
        *,
        lease: CredentialLease,
        action: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._record_audit(
            AuditEvent(
                type=ACTION_DENIED_EVENT,
                actor_id=lease.agent_id,
                target_id=lease.credential_id,
                decision="denied",
                reason=reason,
                metadata={"action": action, **(metadata or {})},
            )
        )

    def _action_denied_event(
        self,
        *,
        lease: CredentialLease,
        action: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            type=ACTION_DENIED_EVENT,
            actor_id=lease.agent_id,
            target_id=lease.credential_id,
            decision="denied",
            reason=reason,
            metadata={"action": action, **(metadata or {})},
        )

    def _record_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._audit_events.append(event)
