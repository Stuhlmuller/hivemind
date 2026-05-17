from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread
import unittest

from hivemind.config import IntentReviewerConfig
from hivemind.credentials import CredentialError, CredentialService, CredentialVault
from hivemind.models import CredentialPolicy, LeaseStatus
from hivemind.policy import PolicyEngine, ProviderIntentReviewDecision, ProviderIntentReviewRequest


def make_service(service_class: type[CredentialService] = CredentialService) -> CredentialService:
    vault = CredentialVault()
    vault.add(
        credential_id="github.main",
        name="GitHub Main",
        provider="github",
        secret_ref="env://GITHUB_TOKEN",  # nosec B106
        policy=CredentialPolicy(
            allowed_agents=frozenset({"agent.scout"}),
            allowed_actions=frozenset({"read_repo"}),
            max_ttl_seconds=60,
        ),
    )
    return service_class(vault)


class RecordingProviderReviewer:
    def __init__(self, *, allowed: bool = True, reason: str = "provider reviewer approved the request") -> None:
        self.allowed = allowed
        self.reason = reason
        self.requests: list[ProviderIntentReviewRequest] = []

    def review(self, request: ProviderIntentReviewRequest) -> ProviderIntentReviewDecision:
        self.requests.append(request)
        return ProviderIntentReviewDecision(allowed=self.allowed, reason=self.reason)


class CredentialServiceTests(unittest.TestCase):
    def test_issues_short_lived_scoped_lease_without_exposing_secret(self) -> None:
        service = make_service()

        lease = service.request_lease(
            credential_id="github.main",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for issue triage",
            ttl_seconds=30,
        )

        public = lease.public_view()
        self.assertTrue(lease.is_active())
        self.assertEqual(public["action"], "read_repo")
        self.assertNotIn("env://GITHUB_TOKEN", public.values())
        self.assertTrue(public["token_preview"].endswith("..."))

    def test_denies_agents_outside_policy(self) -> None:
        service = make_service()

        with self.assertRaisesRegex(CredentialError, "agent is not allowed"):
            service.request_lease(
                credential_id="github.main",
                agent_id="agent.builder",
                action="read_repo",
                intent="Read repository metadata for issue triage",
            )

    def test_denies_actions_outside_policy(self) -> None:
        service = make_service()

        with self.assertRaisesRegex(CredentialError, "outside this credential policy"):
            service.request_lease(
                credential_id="github.main",
                agent_id="agent.scout",
                action="delete_repo",
                intent="Delete the repository because the task asked for it",
            )

    def test_vault_rejects_invalid_secret_refs(self) -> None:
        vault = CredentialVault()

        for secret_ref in ("ghp_raw_secret_value", "https://example.com/token", "env://"):
            with self.subTest(secret_ref=secret_ref):
                with self.assertRaisesRegex(CredentialError, "secret_ref must use"):
                    vault.add(
                        credential_id=f"cred_{secret_ref.replace(':', '_')}",
                        name="Bad Ref",
                        provider="github",
                        secret_ref=secret_ref,
                        policy=CredentialPolicy(
                            allowed_agents=frozenset({"agent.scout"}),
                            allowed_actions=frozenset({"read_repo"}),
                            max_ttl_seconds=60,
                        ),
                    )

    def test_vault_rejects_broker_generated_secret_refs(self) -> None:
        vault = CredentialVault()

        with self.assertRaisesRegex(CredentialError, "secret:// refs are broker-generated"):
            vault.add(
                credential_id="cred_forged_broker_secret",
                name="Forged Broker Secret",
                provider="github",
                secret_ref="secret://cred_existing",  # nosec B106
                policy=CredentialPolicy(
                    allowed_agents=frozenset({"agent.scout"}),
                    allowed_actions=frozenset({"read_repo"}),
                    max_ttl_seconds=60,
                ),
            )

    def test_vault_rejects_managed_secret_kind_for_external_refs(self) -> None:
        vault = CredentialVault()

        with self.assertRaisesRegex(CredentialError, "managed_secret metadata is broker-generated"):
            vault.add(
                credential_id="cred_forged_managed_secret",
                name="Forged Managed Secret",
                provider="github",
                secret_ref="env://GITHUB_TOKEN",  # nosec B106
                policy=CredentialPolicy(
                    allowed_agents=frozenset({"agent.scout"}),
                    allowed_actions=frozenset({"read_repo"}),
                    max_ttl_seconds=60,
                ),
                metadata={"credential_kind": "managed_secret"},
            )

    def test_lease_only_allows_matching_action(self) -> None:
        service = make_service()
        lease = service.request_lease(
            credential_id="github.main",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for issue triage",
        )

        with self.assertRaisesRegex(CredentialError, "does not allow"):
            service.perform_action(
                lease_token=lease.token,
                action="delete_repo",
                payload={"repo": "example"},
            )

        event = service.audit_events()[-1]
        self.assertEqual(event.type, "credential.action.denied")
        self.assertEqual(event.decision, "denied")

    def test_successful_action_consumes_lease_and_blocks_replay(self) -> None:
        service = make_service()
        lease = service.request_lease(
            credential_id="github.main",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for issue triage",
        )

        result = service.perform_action(
            lease_token=lease.token,
            action="read_repo",
            payload={"repo": "example"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(service.list_leases()[0].status, LeaseStatus.REVOKED)

        with self.assertRaisesRegex(CredentialError, "expired or revoked"):
            service.perform_action(
                lease_token=lease.token,
                action="read_repo",
                payload={"repo": "example"},
            )

        event = service.audit_events()[-1]
        self.assertEqual(event.type, "credential.action.denied")
        self.assertEqual(event.reason, "credential lease is expired or revoked")

    def test_agent_lease_request_limit_denies_second_request(self) -> None:
        vault = CredentialVault()
        vault.add(
            credential_id="github.limited",
            name="GitHub Limited",
            provider="github",
            secret_ref="env://GITHUB_TOKEN",  # nosec B106
            policy=CredentialPolicy(
                allowed_agents=frozenset({"agent.scout"}),
                allowed_actions=frozenset({"read_repo"}),
                agent_lease_limit=1,
                rate_limit_window_seconds=60,
            ),
        )
        service = CredentialService(vault)

        service.request_lease(
            credential_id="github.limited",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for issue triage",
        )

        with self.assertRaisesRegex(CredentialError, "agent lease request rate limit exceeded"):
            service.request_lease(
                credential_id="github.limited",
                agent_id="agent.scout",
                action="read_repo",
                intent="Read repository metadata for follow-up triage",
            )

        event = service.audit_events()[-1]
        self.assertEqual(event.type, "credential.lease.denied")
        self.assertEqual(event.metadata["rate_limit"], "agent_lease_limit")
        self.assertNotIn("GITHUB_TOKEN", str(event.public_view()))

    def test_agent_lease_request_limit_runs_before_provider_intent_review(self) -> None:
        vault = CredentialVault()
        vault.add(
            credential_id="github.provider-limited",
            name="GitHub Provider Limited",
            provider="github",
            secret_ref="env://GITHUB_TOKEN",  # nosec B106
            policy=CredentialPolicy(
                allowed_agents=frozenset({"agent.scout"}),
                allowed_actions=frozenset({"read_repo"}),
                agent_lease_limit=1,
                rate_limit_window_seconds=60,
            ),
        )
        reviewer = RecordingProviderReviewer()
        service = CredentialService(
            vault,
            PolicyEngine(
                IntentReviewerConfig(
                    provider="openrouter",
                    model="anthropic/claude-sonnet-4",
                    credential_ref="env://OPENROUTER_API_KEY",
                ),
                provider_reviewers={"openrouter": reviewer},
            ),
        )

        service.request_lease(
            credential_id="github.provider-limited",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for provider-backed issue triage",
        )

        with self.assertRaisesRegex(CredentialError, "agent lease request rate limit exceeded"):
            service.request_lease(
                credential_id="github.provider-limited",
                agent_id="agent.scout",
                action="read_repo",
                intent="Read repository metadata for repeated provider-backed triage",
            )

        self.assertEqual(len(reviewer.requests), 1)

    def test_agent_lease_request_limit_counts_provider_review_denials(self) -> None:
        vault = CredentialVault()
        vault.add(
            credential_id="github.provider-denied-limited",
            name="GitHub Provider Denied Limited",
            provider="github",
            secret_ref="env://GITHUB_TOKEN",  # nosec B106
            policy=CredentialPolicy(
                allowed_agents=frozenset({"agent.scout"}),
                allowed_actions=frozenset({"read_repo"}),
                agent_lease_limit=1,
                rate_limit_window_seconds=60,
            ),
        )
        reviewer = RecordingProviderReviewer(allowed=False, reason="provider reviewer denied the request")
        service = CredentialService(
            vault,
            PolicyEngine(
                IntentReviewerConfig(
                    provider="openrouter",
                    model="anthropic/claude-sonnet-4",
                    credential_ref="env://OPENROUTER_API_KEY",
                ),
                provider_reviewers={"openrouter": reviewer},
            ),
        )

        with self.assertRaisesRegex(CredentialError, "openrouter intent reviewer denied the request"):
            service.request_lease(
                credential_id="github.provider-denied-limited",
                agent_id="agent.scout",
                action="read_repo",
                intent="Read repository metadata for provider-backed issue triage",
            )
        with self.assertRaisesRegex(CredentialError, "agent lease request rate limit exceeded"):
            service.request_lease(
                credential_id="github.provider-denied-limited",
                agent_id="agent.scout",
                action="read_repo",
                intent="Read repository metadata for repeated provider-backed triage",
            )

        self.assertEqual(len(reviewer.requests), 1)
        event = service.audit_events()[-1]
        self.assertEqual(event.type, "credential.lease.denied")
        self.assertEqual(event.metadata["rate_limit"], "agent_lease_limit")

    def test_credential_action_limit_denies_before_second_action(self) -> None:
        vault = CredentialVault()
        vault.add(
            credential_id="github.action-limited",
            name="GitHub Action Limited",
            provider="github",
            secret_ref="env://GITHUB_TOKEN",  # nosec B106
            policy=CredentialPolicy(
                allowed_agents=frozenset({"agent.scout"}),
                allowed_actions=frozenset({"read_repo"}),
                credential_action_limit=1,
                rate_limit_window_seconds=60,
            ),
        )
        service = CredentialService(vault)
        first = service.request_lease(
            credential_id="github.action-limited",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for issue triage",
        )
        second = service.request_lease(
            credential_id="github.action-limited",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for follow-up triage",
        )

        service.perform_action(lease_token=first.token, action="read_repo", payload={"repo": "example"})

        with self.assertRaisesRegex(CredentialError, "credential action rate limit exceeded"):
            service.perform_action(lease_token=second.token, action="read_repo", payload={"repo": "example"})

        event = service.audit_events()[-1]
        self.assertEqual(event.type, "credential.action.denied")
        self.assertEqual(event.metadata["rate_limit"], "credential_action_limit")
        self.assertEqual(service.list_leases()[1].status, LeaseStatus.ACTIVE)

    def test_concurrent_action_limit_check_consumes_and_audits_atomically(self) -> None:
        start_barrier = Barrier(3)
        success_audit_barrier = Barrier(2)

        class DelayedSuccessAuditCredentialService(CredentialService):
            def _record_audit(self, event):
                if event.type == "credential.action.performed":
                    success_audit_barrier.wait(timeout=5)
                super()._record_audit(event)

        vault = CredentialVault()
        vault.add(
            credential_id="github.action-race-limited",
            name="GitHub Action Race Limited",
            provider="github",
            secret_ref="env://GITHUB_TOKEN",  # nosec B106
            policy=CredentialPolicy(
                allowed_agents=frozenset({"agent.scout"}),
                allowed_actions=frozenset({"read_repo"}),
                credential_action_limit=1,
                rate_limit_window_seconds=60,
            ),
        )
        service = DelayedSuccessAuditCredentialService(vault)
        leases = [
            service.request_lease(
                credential_id="github.action-race-limited",
                agent_id="agent.scout",
                action="read_repo",
                intent=f"Read repository metadata for concurrent action {index}",
            )
            for index in range(2)
        ]
        results: list[dict[str, str | bool]] = []
        errors: list[str] = []

        def perform(lease_token: str) -> None:
            start_barrier.wait(timeout=5)
            try:
                results.append(
                    service.perform_action(
                        lease_token=lease_token,
                        action="read_repo",
                        payload={"repo": "example"},
                    )
                )
            except CredentialError as exc:
                errors.append(str(exc))

        threads = [Thread(target=perform, args=(lease.token,)) for lease in leases]
        for thread in threads:
            thread.start()
        start_barrier.wait(timeout=5)
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(errors, ["credential action rate limit exceeded"])
        action_events = [event for event in service.audit_events() if event.type.startswith("credential.action.")]
        self.assertCountEqual(
            [event.type for event in action_events],
            ["credential.action.performed", "credential.action.denied"],
        )

    def test_concurrent_successful_action_consumes_lease_once(self) -> None:
        start_barrier = Barrier(3)
        lookup_barrier = Barrier(2)

        class RacingCredentialService(CredentialService):
            def _find_lease_by_token(self, lease_token: str):
                lease = super()._find_lease_by_token(lease_token)
                lookup_barrier.wait(timeout=5)
                return lease

        service = make_service(RacingCredentialService)
        lease = service.request_lease(
            credential_id="github.main",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for issue triage",
        )

        results: list[dict[str, str | bool]] = []
        errors: list[str] = []

        def perform() -> None:
            start_barrier.wait(timeout=5)
            try:
                results.append(
                    service.perform_action(
                        lease_token=lease.token,
                        action="read_repo",
                        payload={"repo": "example"},
                    )
                )
            except CredentialError as exc:
                errors.append(str(exc))

        threads = [Thread(target=perform) for _ in range(2)]
        for thread in threads:
            thread.start()
        start_barrier.wait(timeout=5)
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(errors, ["credential lease is expired or revoked"])
        self.assertEqual(service.list_leases()[0].status, LeaseStatus.REVOKED)

        action_events = [event for event in service.audit_events() if event.type.startswith("credential.action.")]
        self.assertEqual(len(action_events), 2)
        self.assertCountEqual(
            [event.type for event in action_events],
            ["credential.action.performed", "credential.action.denied"],
        )

    def test_expired_lease_cannot_perform_action(self) -> None:
        service = make_service()
        lease = service.request_lease(
            credential_id="github.main",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for issue triage",
        )

        self.assertFalse(lease.is_active(datetime.now(timezone.utc) + timedelta(minutes=2)))

    def test_pending_approval_lease_cannot_perform_action_until_approved(self) -> None:
        vault = CredentialVault()
        vault.add(
            credential_id="github.writer",
            name="GitHub Writer",
            provider="github",
            secret_ref="env://GITHUB_WRITE_TOKEN",  # nosec B106
            policy=CredentialPolicy(
                allowed_agents=frozenset({"agent.scout"}),
                allowed_actions=frozenset({"open_issue"}),
                approval_required_actions=frozenset({"open_issue"}),
                max_ttl_seconds=90,
            ),
        )
        service = CredentialService(vault)

        pending = service.request_lease(
            credential_id="github.writer",
            agent_id="agent.scout",
            action="open_issue",
            intent="Open an audited issue for a verified regression.",
            ttl_seconds=120,
        )

        self.assertEqual(pending.public_view()["status"], "pending")
        with self.assertRaisesRegex(CredentialError, "pending approval"):
            service.perform_action(
                lease_token=pending.token,
                action="open_issue",
                payload={"repo": "hivemind"},
            )

        approved = service.approve_lease(lease_id=pending.id, approved_by="user.admin")
        result = service.perform_action(
            lease_token=approved.token,
            action="open_issue",
            payload={"repo": "hivemind"},
        )

        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
