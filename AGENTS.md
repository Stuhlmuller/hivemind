# Hivemind Project Notes

Hivemind is an open-source, security-focused agent runtime that runs as a
single container. It can spawn many subagents that are able to take actions,
but credentials must never live inside those agents or be handed to them
directly.

The central design principle is strict credential separation. Credentials are
configured independently from agents, kept away from agent execution contexts,
and exposed only through the credentials service. When an agent needs to use a
credential, it sends a request to that service. The service validates the
agent's intent, decides whether the requested action is allowed, and issues a
short-lived, narrowly scoped lease for that exact use.

All credentials should be just-in-time credentials. Each use should be scoped to
the smallest useful action surface, have a short TTL, and be controlled by
user-configured policy. Agents should receive capabilities, not raw secrets.

Users can configure any AI model/provider they want, including Codex, Claude,
OpenRouter, Gemini, Bedrock, Hugging Face, Ollama, or subscription-backed OAuth
credentials. Provider credentials belong in the credentials service and follow
the same JIT/scoped-use model as every other credential.

The product theme is bees and beehives. Bees swarm, coordinate, and communicate
efficiently with low-context actionable messages. Hivemind agents should follow
the same pattern: brief, useful communication; explicit task intent; and
coordinated action through controlled capability handoffs.

Development rule: commit often. Prefer small, intentional commits that preserve
working checkpoints after each meaningful feature, fix, or security boundary
change.
