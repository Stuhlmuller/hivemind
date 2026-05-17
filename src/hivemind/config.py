from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IntentReviewerConfig:
    provider: str = "local"
    model: str = "deterministic-policy"
    credential_ref: str | None = None

    def public_view(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "credential_ref": self.credential_ref,
        }


@dataclass(frozen=True)
class HivemindConfig:
    intent_reviewer: IntentReviewerConfig

    @classmethod
    def from_env(cls) -> "HivemindConfig":
        return cls(
            intent_reviewer=IntentReviewerConfig(
                provider=os.getenv("HIVEMIND_INTENT_REVIEWER_PROVIDER", "local"),
                model=os.getenv("HIVEMIND_INTENT_REVIEWER_MODEL", "deterministic-policy"),
                credential_ref=os.getenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF"),
            )
        )

    def public_view(self) -> dict[str, Any]:
        return {"intent_reviewer": self.intent_reviewer.public_view()}

