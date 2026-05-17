## Subagent Delegation

1. For any run that results in substantive backlog analysis, code changes, CI triage, or PR review, spawn at least one bounded subagent if delegation is available in this Codex runtime.
2. Keep the top-level loop responsible for issue or PR selection, worktree and branch ownership, final validation synthesis, commits, pushes, issue updates, and PR or merge actions.
3. Prefer an explorer-style subagent for reconnaissance, codepath tracing, or log inspection before you edit.
4. Prefer a worker-style subagent only for disjoint write scopes such as tests, docs, or a separate file slice that will not conflict with the main loop or another subagent.
5. Give every subagent a concrete task, clear output, and explicit file or responsibility ownership.
6. Do not let subagents choose a different issue, merge PRs, or make broad autonomous priority decisions.
7. Only the main-branch issue scout lane may use the Codex browser tool. Do not use it in worker, reviewer, feature-requester, or general-development subagents.
8. If subagent delegation is unavailable in this runtime, continue locally and note that limitation in the run result instead of pretending delegation happened.
