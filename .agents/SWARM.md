# GitHub Swarm Loops

The repository now has a multi-loop GitHub automation suite that complements the original Ralph workflow.

## Roles

- `scout`: audits the project and backlog, then opens only high-signal missing issues.
- `worker-a`: implements odd-numbered issues in its own dedicated worktree.
- `worker-b`: implements even-numbered issues in its own dedicated worktree.
- `pr-shepherd`: fixes clear CI failures on idle PR branches and merges ready PRs.

## Start and monitor

```bash
.agents/swarm.sh start
.agents/swarm.sh status
.agents/swarm.sh logs worker-a
.agents/swarm.sh stop
```

## Defaults

- Worktrees live under `${TMPDIR:-/tmp}/hivemind-swarm-worktrees/<repo-name>/`.
- Logs and pid files live under `.agents/runtime/swarm/`.
- `worker-a` and `worker-b` split the backlog deterministically by odd/even issue number so they do not race for the same issue.
- Worker loops open or update PRs, but the PR shepherd is responsible for merges and cross-branch CI cleanup.
- After a PR is merged or closed, the worker or shepherd loop should clean up that branch in its dedicated worktree and return to the default-branch base before picking new work.

## Useful env overrides

- `HIVEMIND_SWARM_WORKTREE_ROOT`: override the base directory for loop worktrees.
- `HIVEMIND_SWARM_RUNTIME_ROOT`: override where logs and pid files are stored.
- `HIVEMIND_SCOUT_SLEEP_SECONDS`
- `HIVEMIND_WORKER_A_SLEEP_SECONDS`
- `HIVEMIND_WORKER_B_SLEEP_SECONDS`
- `HIVEMIND_PR_SHEPHERD_SLEEP_SECONDS`
- `HIVEMIND_SCOUT_MAX_RUNS`
- `HIVEMIND_WORKER_A_MAX_RUNS`
- `HIVEMIND_WORKER_B_MAX_RUNS`
- `HIVEMIND_PR_SHEPHERD_MAX_RUNS`
