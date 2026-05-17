from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


AGENT_PROVIDER_ALIASES = {
    "anthropic": "claude",
    "hugging-face": "huggingface",
    "open-router": "openrouter",
}
CREDENTIAL_OPTIONAL_AGENT_PROVIDERS = {"local", "ollama"}


def normalize_agent_provider_id(provider: str | None) -> str:
    normalized = (provider or "local").strip().lower().replace("_", "-").replace(" ", "-")
    return AGENT_PROVIDER_ALIASES.get(normalized, normalized or "local")


@dataclass(frozen=True)
class ProviderMessage:
    role: str
    content: str

    def public_view(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ProviderToolRequest:
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def public_view(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": dict(self.arguments)}


@dataclass(frozen=True)
class ProviderRunRequest:
    provider: str
    model: str
    prompt: str
    system_prompt: str = ""
    messages: Sequence[ProviderMessage] = field(default_factory=tuple)
    tool_requests: Sequence[ProviderToolRequest] = field(default_factory=tuple)
    credential_id: str | None = None
    credential_action: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderRunResult:
    provider: str
    model: str
    output_text: str
    tool_requests: Sequence[ProviderToolRequest] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def public_view(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "output_text": self.output_text,
            "tool_requests": [tool.public_view() for tool in self.tool_requests],
        }


class AgentProviderAdapter(Protocol):
    def provider_id(self) -> str:
        ...

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        ...


class AgentProviderError(ValueError):
    pass


class MissingAgentProviderAdapterError(AgentProviderError):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"agent provider adapter is not configured: {provider}")


class LocalDeterministicProviderAdapter:
    def provider_id(self) -> str:
        return "local"

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        prompt = request.prompt.strip()
        output = f"local deterministic response for {request.model}: {prompt}"
        return ProviderRunResult(
            provider="local",
            model=request.model,
            output_text=output,
            tool_requests=tuple(request.tool_requests),
            metadata={"adapter": "local-deterministic"},
        )


class AgentProviderRegistry:
    def __init__(self, adapters: Mapping[str, AgentProviderAdapter] | None = None) -> None:
        self._adapters: dict[str, AgentProviderAdapter] = {}
        self.register(LocalDeterministicProviderAdapter())
        for adapter in (adapters or {}).values():
            self.register(adapter)

    def register(self, adapter: AgentProviderAdapter) -> None:
        self._adapters[normalize_agent_provider_id(adapter.provider_id())] = adapter

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        provider_id = normalize_agent_provider_id(request.provider)
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise MissingAgentProviderAdapterError(provider_id)
        return adapter.run(request)

    def has_adapter(self, provider: str) -> bool:
        return normalize_agent_provider_id(provider) in self._adapters
