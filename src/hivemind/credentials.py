from __future__ import annotations

from dataclasses import replace
from threading import RLock
from typing import Any, cast

from hivemind.models import (
    AuditEvent,
    CredentialLease,
    CredentialPolicy,
    CredentialRecord,
    LeaseStatus,
)
from hivemind.policy import PolicyEngine
from hivemind.secret_refs import validate_external_credential_metadata, validate_external_secret_ref
from hivemind.tool_registry import DEFAULT_TOOL_ACTIONS, normalize_tool_action_name, payload_schema_error


LEASE_DENIED_EVENT = "credential.lease.denied"
ACTION_DENIED_EVENT = "credential.action.denied"
LEASE_EXPIRED_OR_REVOKED_REASON = "credential lease is expired or revoked"


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
    def __init__(
        self,
        vault: CredentialVault,
        policy_engine: PolicyEngine | None = None,
        *,
        tool_actions: list[dict[str, Any]] | None = None,
    ) -> None:
        self._vault = vault
        self._policy_engine = policy_engine or PolicyEngine()
        self._tool_actions = {
            normalize_tool_action_name(action["name"]): action
            for action in (tool_actions or list(DEFAULT_TOOL_ACTIONS))
        }
        self._leases: dict[str, CredentialLease] = {}
        self._audit_events: list[AuditEvent] = []
        self._lock = RLock()

    def _tool_action_for_request(self, action: str) -> dict[str, Any]:
        normalized_action = normalize_tool_action_name(action)
        if not normalized_action:
            raise CredentialError("tool action is required")
        try:
            return self._tool_actions[normalized_action]
        except KeyError as exc:
            raise CredentialError(f"unknown tool action: {normalized_action}") from exc

    def _record_action_denial(
        self,
        lease: CredentialLease,
        *,
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
                metadata={"action": action, "lease_id": lease.id, **(metadata or {})},
            )
        )

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
        try:
            tool_action = self._tool_action_for_request(action)
        except CredentialError as exc:
            self._record_audit(
                AuditEvent(
                    type=LEASE_DENIED_EVENT,
                    actor_id=agent_id,
                    target_id=credential_id,
                    decision="denied",
                    reason=str(exc),
                    metadata={"action": normalize_tool_action_name(action)},
                )
            )
            raise
        credential_action = tool_action["required_credential_action"]
        review = self._policy_engine.review_intent(
            credential=credential,
            agent_id=agent_id,
            action=credential_action,
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
                    metadata={"action": tool_action["name"], "credential_action": credential_action},
                )
            )
            raise CredentialError(review.reason)

        requested_ttl = ttl_seconds or credential.policy.max_ttl_seconds
        ttl = min(requested_ttl, credential.policy.max_ttl_seconds)
        requires_approval = credential_action in credential.policy.approval_required_actions
        lease = (
            CredentialLease.request_approval(
                credential_id=credential.id,
                agent_id=agent_id,
                action=tool_action["name"],
                intent=intent,
                ttl_seconds=ttl,
            )
            if requires_approval
            else CredentialLease.issue(
                credential_id=credential.id,
                agent_id=agent_id,
                action=tool_action["name"],
                intent=intent,
                ttl_seconds=ttl,
            )
        )

        with self._lock:
            self._leases[lease.id] = lease

        self._record_audit(
            AuditEvent(
                type="credential.lease.pending" if requires_approval else "credential.lease.issued",
                actor_id=agent_id,
                target_id=credential_id,
                decision="pending" if requires_approval else "allowed",
                reason="action requires operator approval" if requires_approval else review.reason,
                metadata={
                    "action": lease.action,
                    "credential_action": credential_action,
                    "ttl_seconds": ttl,
                    "lease_id": lease.id,
                },
            )
        )
        return lease

    def perform_action(self, *, lease_token: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_action = normalize_tool_action_name(action)
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

        if lease.status == LeaseStatus.PENDING:
            self._record_action_denial(lease, action=normalized_action, reason="credential lease is pending approval")
            raise CredentialError("credential lease is pending approval")
        if lease.status == LeaseStatus.DENIED:
            self._record_action_denial(lease, action=normalized_action, reason="credential lease request was denied")
            raise CredentialError("credential lease request was denied")
        if not lease.is_active():
            self._record_action_denial(lease, action=normalized_action, reason=LEASE_EXPIRED_OR_REVOKED_REASON)
            raise CredentialError(LEASE_EXPIRED_OR_REVOKED_REASON)
        if lease.action != normalized_action:
            self._record_action_denial(lease, action=normalized_action, reason="credential lease does not allow this action")
            raise CredentialError("credential lease does not allow this action")

        try:
            tool_action = self._tool_action_for_request(normalized_action)
        except CredentialError as exc:
            self._record_action_denial(lease, action=normalized_action, reason=str(exc))
            raise
        payload_error = payload_schema_error(tool_action["input_schema"], payload)
        if payload_error is not None:
            self._record_action_denial(
                lease,
                action=normalized_action,
                reason=payload_error,
                metadata={"payload_keys": sorted(payload.keys())},
            )
            raise CredentialError(payload_error)

        consumed_lease = self._consume_active_lease(lease=lease, action=normalized_action)
        if consumed_lease is None:
            self._record_action_denial(lease, action=normalized_action, reason=LEASE_EXPIRED_OR_REVOKED_REASON)
            raise CredentialError(LEASE_EXPIRED_OR_REVOKED_REASON)

        credential = self._vault.get(consumed_lease.credential_id)
        self._record_audit(
            AuditEvent(
                type="credential.action.performed",
                actor_id=consumed_lease.agent_id,
                target_id=credential.id,
                decision="allowed",
                reason="action matched active credential lease",
                metadata={
                    "action": normalized_action,
                    "credential_action": tool_action["required_credential_action"],
                    "payload_keys": sorted(payload.keys()),
                },
            )
        )
        return {
            "ok": True,
            "provider": credential.provider,
            "credential_id": credential.id,
            "action": normalized_action,
            "credential_action": tool_action["required_credential_action"],
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

    def _consume_active_lease(self, *, lease: CredentialLease, action: str) -> CredentialLease | None:
        with self._lock:
            current = self._leases.get(lease.id)
            if current is None or not current.is_active():
                return None
            if current.action != action:
                return current
            consumed = replace(current, status=LeaseStatus.REVOKED)
            self._leases[lease.id] = consumed
            return consumed

    def _record_action_denied(self, *, lease: CredentialLease, action: str, reason: str) -> None:
        self._record_audit(
            AuditEvent(
                type=ACTION_DENIED_EVENT,
                actor_id=lease.agent_id,
                target_id=lease.credential_id,
                decision="denied",
                reason=reason,
                metadata={"action": action},
            )
        )

    def _record_audit(self, event: AuditEvent) -> None:
        with self._lock:
            self._audit_events.append(event)
