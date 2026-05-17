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
Commit signing rule: every git commit for this repository must be signed.
Unsigned commits are not acceptable because policy checks reject them. If commit
signing fails, stop and fix signing before creating or pushing more commits.

Bootstrap rule: before starting repo work or spawning a repo agent, follow
`.agents/PROMPT.md`. Keep `flake.nix` and `.agents/TOOLS.md` aligned with the
CLI set for the run, inspect issues with `gh issue list --state all --limit
100` before choosing issue-driven work, use one issue per branch and PR, and
state the blocker explicitly if GitHub access is unavailable in the current
environment.

Publish rule: when explicitly asked to stage, commit, push, and open a PR in
one flow, use the `yeet` skill instead of improvising an ad hoc git/gh
sequence. Keep its safety checks around scope confirmation, intentional
staging, and draft PR creation. Do not treat `yeet` as an automatic merge
shortcut; merge only after checks pass and the review state is acceptable.

Quality rule: before finishing code changes, run Qlty from the repo root against
the scope you touched. At minimum run `qlty check` on changed files. Use
`qlty check --all` for broad refactors or release-facing changes, and run
`qlty smells` when you touched larger structures or risked duplication. Fix the
issues you introduced or explicitly call out remaining findings in the final
handoff.
Security audit rule: after every implementation change, use the project-local
`hivemind-security-audit` skill before finalizing. Treat findings around auth,
sessions, credential separation, JIT leases, audit logs, persistence, frontend
secret exposure, and policy enforcement as blockers until fixed or explicitly
accepted by the user. Every implementation should be secure by default,
deny-by-default, and well-architected with narrow trust boundaries and focused
verification.

Frontend rule: before changing the Hivemind UI, use the project-local
`hivemind-ui-no-slop` skill. The UI must feel technical, open-source,
self-hosted, agentic, security-focused, and new; reject generic SaaS styling,
marketing heroes, decorative bee filler, and amber/beige theme wash.

Skill capture rule: use the project-local `hivemind-skill-capture` skill when a
new ask creates a durable project rule, workflow, taste preference, security
boundary, or implementation pattern. Prefer small project-local skills so future
agents can replicate Hivemind work with little context.

Idea rule: use the project-local `hivemind-idea-issues` skill when the user
asks for new Hivemind ideas, backlog or roadmap suggestions, gap analysis, or
"what should we build next" without asking to start coding. This workflow must
create GitHub issues only and must not start implementation, branches, or PRs
unless the user explicitly changes scope.

Git workflow rule: use the project-local `hivemind-git-commits` skill before
staging, committing, or pushing Hivemind changes so branch scope, commit
signing, and verification stay consistent.
Checkpoint rule: use the project-local `hivemind-branch-checkpoints` skill
during multi-step work on any Hivemind branch. Prefer small signed checkpoint
commits after each meaningful feature slice, security boundary change,
verification milestone, or coherent refactor instead of waiting until the end
of the branch.

Auth rule: use the project-local `hivemind-homelab-auth` skill before changing
setup, login, sessions, or account flows. Hivemind is self-hosted homelab
software; use username/password local auth, not email-first SaaS account flows.

Ralph rule: use the project-local `hivemind-ralph-loop` skill before changing
`.agents/ralph.sh` or `.agents/PROMPT.md`. Ralph is a GitHub-driven loop: it
must require working `gh`, run with GitHub-capable network access, move work
onto `issue-<number>-<slug>` branches inside dedicated git worktrees, never
reuse the primary checkout or repurpose a worktree via local issue-branch
checkout, and fail when the wrapper cannot verify those rules.
