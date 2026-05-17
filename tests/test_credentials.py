from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from hivemind.credentials import CredentialError, CredentialService, CredentialVault
from hivemind.models import CredentialPolicy


def make_service() -> CredentialService:
    vault = CredentialVault()
    vault.add(
        credential_id="github.main",
        name="GitHub Main",
        provider="github",
        secret_ref="env://GITHUB_TOKEN",
        policy=CredentialPolicy(
            allowed_agents=frozenset({"agent.scout"}),
            allowed_actions=frozenset({"read_repo"}),
            max_ttl_seconds=60,
        ),
    )
    return CredentialService(vault)


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

    def test_expired_lease_cannot_perform_action(self) -> None:
        service = make_service()
        lease = service.request_lease(
            credential_id="github.main",
            agent_id="agent.scout",
            action="read_repo",
            intent="Read repository metadata for issue triage",
        )

        self.assertFalse(lease.is_active(datetime.now(timezone.utc) + timedelta(minutes=2)))


if __name__ == "__main__":
    unittest.main()

