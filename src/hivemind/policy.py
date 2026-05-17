from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from hivemind.config import IntentReviewerConfig
from hivemind.models import CredentialRecord, IntentReview


@dataclass(frozen=True)
class PolicyReviewInput:
    credential_id: str
    credential_provider: str
    allowed_agents: frozenset[str]
    allowed_actions: frozenset[str]
    require_intent: bool
    agent_id: str
    action: str
    intent: str
    credential_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderIntentReviewRequest:
    reviewer_provider: str
    reviewer_model: str
    reviewer_credential_ref: str
    credential_id: str
    credential_provider: str
    allowed_actions: tuple[str, ...]
    require_intent: bool
    agent_id: str
    action: str
    intent: str
    credential_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderIntentReviewDecision:
    allowed: bool
    reason: str


class ProviderIntentReviewer(Protocol):
    def review(self, request: ProviderIntentReviewRequest) -> ProviderIntentReviewDecision:
        ...


class ProviderIntentReviewerError(ValueError):
    pass


class PolicyEngine:
    """Local intent and scope validator.

    The broker always applies deterministic policy checks first. When a
    non-local reviewer is configured, a provider adapter can make the final
    allow/deny decision without changing the lease contract.
    """

    def __init__(
        self,
        intent_reviewer: IntentReviewerConfig | None = None,
        *,
        provider_reviewers: Mapping[str, ProviderIntentReviewer] | None = None,
    ) -> None:
        self.intent_reviewer = intent_reviewer or IntentReviewerConfig()
        self._provider_reviewers = dict(provider_reviewers or {})

    def review_intent(
        self,
        *,
        credential: CredentialRecord,
        agent_id: str,
        action: str,
        intent: str,
    ) -> IntentReview:
        return self.review_request(
            PolicyReviewInput(
                credential_id=credential.id,
                credential_provider=credential.provider,
                allowed_agents=credential.policy.allowed_agents,
                allowed_actions=credential.policy.allowed_actions,
                require_intent=credential.policy.require_intent,
                agent_id=agent_id,
                action=action,
                intent=intent,
                credential_metadata=credential.metadata,
            )
        )

    def review_request(self, request: PolicyReviewInput) -> IntentReview:
        normalized_action = request.action.strip().lower()
        normalized_intent = request.intent.strip()
        deterministic_review = self._review_deterministic_policy(
            request=request,
            normalized_action=normalized_action,
            normalized_intent=normalized_intent,
        )

        if not deterministic_review.allowed or self.intent_reviewer.provider_id() == "local":
            return deterministic_review

        return self._review_with_provider(
            request=request,
            normalized_action=normalized_action,
            normalized_intent=normalized_intent,
        )

    def _review_deterministic_policy(
        self,
        *,
        request: PolicyReviewInput,
        normalized_action: str,
        normalized_intent: str,
    ) -> IntentReview:
        if request.agent_id not in request.allowed_agents:
            return IntentReview(False, "agent is not allowed to use this credential", normalized_action)

        if normalized_action not in request.allowed_actions:
            return IntentReview(False, "action is outside this credential policy", normalized_action)

        if request.require_intent and len(normalized_intent) < 12:
            return IntentReview(False, "intent is too short to authorize", normalized_action)

        return IntentReview(True, "intent and scope satisfy policy", normalized_action)

    def _review_with_provider(
        self,
        *,
        request: PolicyReviewInput,
        normalized_action: str,
        normalized_intent: str,
    ) -> IntentReview:
        if not self.intent_reviewer.credential_ref:
            return IntentReview(False, "intent reviewer credential_ref is required for provider-backed review", normalized_action)

        reviewer = self._provider_reviewers.get(self.intent_reviewer.provider_id())
        if reviewer is None:
            return IntentReview(
                False,
                f"intent reviewer provider is not configured: {self.intent_reviewer.provider}",
                normalized_action,
            )

        provider_request = ProviderIntentReviewRequest(
            reviewer_provider=self.intent_reviewer.provider_id(),
            reviewer_model=self.intent_reviewer.model,
            reviewer_credential_ref=self.intent_reviewer.credential_ref,
            credential_id=request.credential_id,
            credential_provider=request.credential_provider,
            allowed_actions=tuple(sorted(request.allowed_actions)),
            require_intent=request.require_intent,
            agent_id=request.agent_id,
            action=normalized_action,
            intent=normalized_intent,
            credential_metadata=request.credential_metadata,
        )
        try:
            decision = reviewer.review(provider_request)
        except ProviderIntentReviewerError:
            return IntentReview(False, "intent reviewer provider failed closed", normalized_action)
        except Exception:
            return IntentReview(False, "intent reviewer provider failed closed", normalized_action)

        reason = (
            f"{self.intent_reviewer.provider} intent reviewer "
            + ("approved the request" if decision.allowed else "denied the request")
        )
        return IntentReview(decision.allowed, reason, normalized_action)
