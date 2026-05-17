from __future__ import annotations

from secrets import token_urlsafe
from threading import RLock

from hivemind.models import Agent, AgentStatus


class AgentError(ValueError):
    pass


class AgentService:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._lock = RLock()

    def spawn(self, *, name: str, role: str, provider: str, model: str) -> Agent:
        agent = Agent(
            id=f"agent_{token_urlsafe(8)}",
            name=name,
            role=role,
            provider=provider,
            model=model,
        )
        with self._lock:
            self._agents[agent.id] = agent
        return agent

    def get(self, agent_id: str) -> Agent:
        with self._lock:
            try:
                return self._agents[agent_id]
            except KeyError as exc:
                raise AgentError(f"unknown agent: {agent_id}") from exc

    def list(self) -> list[Agent]:
        with self._lock:
            return list(self._agents.values())

    def mark_working(self, agent_id: str) -> Agent:
        return self._replace_status(agent_id, AgentStatus.WORKING)

    def mark_idle(self, agent_id: str) -> Agent:
        return self._replace_status(agent_id, AgentStatus.IDLE)

    def _replace_status(self, agent_id: str, status: AgentStatus) -> Agent:
        agent = self.get(agent_id)
        updated = Agent(
            id=agent.id,
            name=agent.name,
            role=agent.role,
            provider=agent.provider,
            model=agent.model,
            status=status,
        )
        with self._lock:
            self._agents[agent_id] = updated
        return updated

