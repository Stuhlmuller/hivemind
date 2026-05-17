from __future__ import annotations

from hivemind.agents import AgentService
from hivemind.config import HivemindConfig
from hivemind.credentials import CredentialService, CredentialVault
from hivemind.models import CredentialPolicy
from hivemind.policy import PolicyEngine


class HivemindRuntime:
    def __init__(self, config: HivemindConfig | None = None) -> None:
        self.config = config or HivemindConfig.from_env()
        self.agents = AgentService()
        self.vault = CredentialVault()
        self.credentials = CredentialService(
            self.vault,
            policy_engine=PolicyEngine(self.config.intent_reviewer),
        )
        self._seed_default_state()

    def _seed_default_state(self) -> None:
        scout = self.agents.spawn(
            name="Scout",
            role="gather concise context and report actionable findings",
            provider=self.config.intent_reviewer.provider,
            model=self.config.intent_reviewer.model,
        )
        self.vault.add(
            credential_id="demo.github",
            name="Demo GitHub Capability",
            provider="github",
            secret_ref="env://HIVEMIND_DEMO_GITHUB_TOKEN",
            policy=CredentialPolicy(
                allowed_agents=frozenset({scout.id}),
                allowed_actions=frozenset({"read_repo", "open_issue"}),
                max_ttl_seconds=120,
            ),
            metadata={"purpose": "safe local demo credential reference"},
        )
