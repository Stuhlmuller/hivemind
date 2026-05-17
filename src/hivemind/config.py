from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from hivemind.providers import normalize_agent_provider_id
from hivemind.secret_refs import preview_secret_ref, validate_secret_ref

TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
LOCAL_INTENT_REVIEWER_PROVIDER = "local"
INTENT_REVIEWER_PROVIDER_ALIASES = {
    "hugging-face": "huggingface",
    "subscription": "oauth",
    "subscription-backed": "oauth",
}
AGENT_PROVIDER_DEFAULT_MODELS = {
    "local": "deterministic-policy",
    "openai": "operator-configured",
    "codex": "operator-configured",
    "claude": "operator-configured",
    "gemini": "operator-configured",
    "openrouter": "operator-configured",
    "bedrock": "operator-configured",
    "huggingface": "operator-configured",
    "ollama": "operator-configured",
}
AGENT_PROVIDER_ENV_NAMES = {
    "local": "LOCAL",
    "openai": "OPENAI",
    "codex": "CODEX",
    "claude": "CLAUDE",
    "gemini": "GEMINI",
    "openrouter": "OPENROUTER",
    "bedrock": "BEDROCK",
    "huggingface": "HUGGINGFACE",
    "ollama": "OLLAMA",
}


def env_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUTHY_ENV_VALUES


def normalize_intent_reviewer_provider(provider: str | None) -> str:
    normalized = (provider or LOCAL_INTENT_REVIEWER_PROVIDER).strip().lower().replace("_", "-").replace(" ", "-")
    return INTENT_REVIEWER_PROVIDER_ALIASES.get(normalized, normalized or LOCAL_INTENT_REVIEWER_PROVIDER)


@dataclass(frozen=True)
class IntentReviewerConfig:
    provider: str = LOCAL_INTENT_REVIEWER_PROVIDER
    model: str = "deterministic-policy"
    credential_ref: str | None = None

    def __post_init__(self) -> None:
        if self.credential_ref:
            validate_secret_ref(self.credential_ref)

    def provider_id(self) -> str:
        return normalize_intent_reviewer_provider(self.provider)

    def public_view(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "credential_ref_preview": preview_secret_ref(self.credential_ref),
        }


@dataclass(frozen=True)
class AgentProviderConfig:
    provider: str
    model: str
    credential_ref: str | None = None

    def __post_init__(self) -> None:
        if self.credential_ref:
            validate_secret_ref(self.credential_ref)

    def provider_id(self) -> str:
        return normalize_agent_provider_id(self.provider)

    def public_view(self) -> dict[str, Any]:
        return {
            "provider": self.provider_id(),
            "model": self.model,
            "credential_ref_preview": preview_secret_ref(self.credential_ref),
        }


def load_agent_provider_configs_from_env() -> dict[str, AgentProviderConfig]:
    configs: dict[str, AgentProviderConfig] = {}
    for provider_id, default_model in AGENT_PROVIDER_DEFAULT_MODELS.items():
        env_name = AGENT_PROVIDER_ENV_NAMES[provider_id]
        model = os.getenv(f"HIVEMIND_AGENT_PROVIDER_{env_name}_MODEL") or default_model
        credential_ref = os.getenv(f"HIVEMIND_AGENT_PROVIDER_{env_name}_CREDENTIAL_REF") or None
        configs[provider_id] = AgentProviderConfig(
            provider=provider_id,
            model=model,
            credential_ref=credential_ref,
        )
    return configs


@dataclass(frozen=True)
class HivemindConfig:
    intent_reviewer: IntentReviewerConfig
    agent_providers: dict[str, AgentProviderConfig] = field(default_factory=load_agent_provider_configs_from_env)
    development_mode: bool = False

    @classmethod
    def from_env(cls) -> "HivemindConfig":
        return cls(
            development_mode=env_flag("HIVEMIND_DEVELOPMENT_MODE", default=False),
            intent_reviewer=IntentReviewerConfig(
                provider=os.getenv("HIVEMIND_INTENT_REVIEWER_PROVIDER") or LOCAL_INTENT_REVIEWER_PROVIDER,
                model=os.getenv("HIVEMIND_INTENT_REVIEWER_MODEL") or "deterministic-policy",
                credential_ref=os.getenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF") or None,
            ),
            agent_providers=load_agent_provider_configs_from_env(),
        )

    def agent_provider(self, provider: str) -> AgentProviderConfig:
        provider_id = normalize_agent_provider_id(provider)
        return self.agent_providers.get(
            provider_id,
            AgentProviderConfig(provider=provider_id, model="operator-configured"),
        )

    def public_view(self) -> dict[str, Any]:
        return {
            "intent_reviewer": self.intent_reviewer.public_view(),
            "agent_providers": [config.public_view() for config in self.agent_providers.values()],
        }
