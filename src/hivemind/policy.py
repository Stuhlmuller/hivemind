from __future__ import annotations

from hivemind.config import IntentReviewerConfig
from hivemind.models import CredentialRecord, IntentReview


class PolicyEngine:
    """Local intent and scope validator.

    This is intentionally deterministic for the initial implementation. The
    next provider-backed version can call the user's configured model here
    while keeping the credential lease contract unchanged.
    """

    def __init__(self, intent_reviewer: IntentReviewerConfig | None = None) -> None:
        self.intent_reviewer = intent_reviewer or IntentReviewerConfig()

    def review_intent(
        self,
        *,
        credential: CredentialRecord,
        agent_id: str,
        action: str,
        intent: str,
    ) -> IntentReview:
        normalized_action = action.strip().lower()
        normalized_intent = intent.strip()

        if agent_id not in credential.policy.allowed_agents:
            return IntentReview(False, "agent is not allowed to use this credential", normalized_action)

        if normalized_action not in credential.policy.allowed_actions:
            return IntentReview(False, "action is outside this credential policy", normalized_action)

        if credential.policy.require_intent and len(normalized_intent) < 12:
            return IntentReview(False, "intent is too short to authorize", normalized_action)

        return IntentReview(True, "intent and scope satisfy policy", normalized_action)
