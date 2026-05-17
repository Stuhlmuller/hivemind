from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from hivemind.secret_refs import preview_secret_ref

TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
LOCAL_INTENT_REVIEWER_PROVIDER = "local"
INTENT_REVIEWER_PROVIDER_ALIASES = {
    "hugging-face": "huggingface",
    "subscription": "oauth",
    "subscription-backed": "oauth",
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

    def provider_id(self) -> str:
        return normalize_intent_reviewer_provider(self.provider)

    def public_view(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "credential_ref_preview": preview_secret_ref(self.credential_ref),
        }


@dataclass(frozen=True)
class HivemindConfig:
    intent_reviewer: IntentReviewerConfig
    development_mode: bool = False

    @classmethod
    def from_env(cls) -> "HivemindConfig":
        return cls(
            development_mode=env_flag("HIVEMIND_DEVELOPMENT_MODE", default=False),
            intent_reviewer=IntentReviewerConfig(
                provider=os.getenv("HIVEMIND_INTENT_REVIEWER_PROVIDER") or LOCAL_INTENT_REVIEWER_PROVIDER,
                model=os.getenv("HIVEMIND_INTENT_REVIEWER_MODEL") or "deterministic-policy",
                credential_ref=os.getenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF") or None,
            )
        )

    def public_view(self) -> dict[str, Any]:
        return {"intent_reviewer": self.intent_reviewer.public_view()}
