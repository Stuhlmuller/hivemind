from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from hivemind.models import (
    AuditEvent,
    CredentialLease,
    CredentialPolicy,
    CredentialRecord,
    LeaseStatus,
)
from hivemind.policy import PolicyEngine
from hivemind.secret_refs import validate_secret_ref


class CredentialError(ValueError):
    pass


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
                secret_ref = validate_secret_ref(secret_ref)
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
        review = self._policy_engine.review_intent(
            credential=credential,
            agent_id=agent_id,
            action=action,
            intent=intent,
        )

        if not review.allowed:
            self._record_audit(
                AuditEvent(
                    type="credential.lease.denied",
                    actor_id=agent_id,
                    target_id=credential_id,
                    decision="denied",
                    reason=review.reason,
                    metadata={"action": action},
                )
            )
            raise CredentialError(review.reason)

        requested_ttl = ttl_seconds or credential.policy.max_ttl_seconds
        ttl = min(requested_ttl, credential.policy.max_ttl_seconds)
        lease = CredentialLease.issue(
            credential_id=credential.id,
            agent_id=agent_id,
            action=review.normalized_action,
            intent=intent,
            ttl_seconds=ttl,
        )

        with self._lock:
            self._leases[lease.id] = lease

        self._record_audit(
            AuditEvent(
                type="credential.lease.issued",
                actor_id=agent_id,
                target_id=credential_id,
                decision="allowed",
                reason=review.reason,
                metadata={"action": lease.action, "ttl_seconds": ttl},
            )
        )
        return lease

    def perform_action(self, *, lease_token: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        lease = self._find_lease_by_token(lease_token)
        normalized_action = action.strip().lower()

        if not lease.is_active():
            raise CredentialError("credential lease is expired or revoked")

        if lease.action != normalized_action:
            raise CredentialError("credential lease does not allow this action")

        credential = self._vault.get(lease.credential_id)
        self._record_audit(
            AuditEvent(
                type="credential.action.performed",
                actor_id=lease.agent_id,
                target_id=credential.id,
                decision="allowed",
                reason="action matched active credential lease",
                metadata={"action": normalized_action, "payload_keys": sorted(payload.keys())},
            )
        )
        return {
            "ok": True,
            "provider": credential.provider,
            "credential_id": credential.id,
            "action": normalized_action,
            "result": "credential action accepted by broker",
        }

    def revoke_lease(self, lease_id: str) -> CredentialLease:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise CredentialError(f"unknown lease: {lease_id}")
            revoked = replace(lease, status=LeaseStatus.REVOKED)
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

    def _record_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._audit_events.append(event)
