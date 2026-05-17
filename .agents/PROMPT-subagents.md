## Subagent Delegation

1. When delegation is available and the run would benefit from parallel reconnaissance or disjoint execution, spawn bounded subagents freely.
2. Keep the top-level loop responsible for issue or PR selection, worktree and branch ownership, final validation synthesis, commits, pushes, issue updates, and PR or merge actions.
3. Prefer an explorer-style subagent for reconnaissance, codepath tracing, or log inspection before you edit.
4. Use as many parallel bounded subagents as the runtime safely supports for concrete, non-overlapping tasks.
5. Prefer a worker-style subagent only for disjoint write scopes such as tests, docs, or a separate file slice that will not conflict with the main loop or another subagent.
6. Give every subagent a concrete task, clear output, and explicit file or responsibility ownership.
7. Do not let subagents choose a different issue, merge PRs, or make broad autonomous priority decisions.
8. Only the main-branch scout agent may use the Codex browser tool. Do not use it in worker, reviewer, feature-requester, beekeeper, or general-development subagents.
9. If subagent delegation is unavailable in this runtime, continue locally and note that limitation in the run result instead of pretending delegation happened.
