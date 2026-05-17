# GitHub Swarm Loops

The repository's supported GitHub automation suite is the multi-loop swarm workflow. The older single-loop wrapper has been retired.

## Roles

- `reviewer-<n>`: audits the project, open PRs, docs, tests, CI, and release posture, then opens grounded follow-up issues.
- `worker-<n>`: implements issues in dedicated worktrees, with deterministic issue sharding across the configured worker count.
- `feature-requester-<n>`: audits product and operator gaps, then opens focused feature backlog issues without starting implementation.
- `scout-<n>`: audits shipped behavior on the default branch and is the only role allowed to use the Codex browser tool.
- `pr-shepherd-<n>`: fixes clear CI failures on idle PR branches and merges ready PRs.
- Every loop prepends a shared subagent delegation policy, and the top-level loop stays responsible for final GitHub mutations.

## Start and monitor

```bash
.agents/swarm.sh start
.agents/swarm.sh start --reviewers 3 --workers 10 --feature-requesters 3 --scouts 1 --pr-shepherds 1
.agents/swarm.sh run
.agents/swarm.sh run --reviewers 5 --workers 16 --feature-requesters 5 --scouts 1 --pr-shepherds 1
.agents/swarm.sh status
.agents/swarm.sh logs worker-1
.agents/swarm.sh logs --follow
.agents/swarm.sh logs --follow worker-1 pr-shepherd-1
.agents/swarm.sh stop
```

For "open laptop and let it keep going" development on macOS:

```bash
.agents/swarm-launchd.sh install
.agents/swarm-launchd.sh install --reviewers 3 --workers 10 --feature-requesters 3 --scouts 1 --pr-shepherds 1
.agents/swarm-launchd.sh status
```

## Defaults

- Worktrees live under `${TMPDIR:-/tmp}/hivemind-swarm-worktrees/<repo-name>/`.
- Logs and pid files live under `.agents/runtime/swarm/`.
- The default fleet is 3 reviewers, 10 workers, 3 feature-requesters, 1 scout, and 1 PR shepherd.
- Worker lanes shard issue ownership with `((issue_number - 1) % worker_count) + 1`.
- PR shepherd lanes shard PR ownership with `((pr_number - 1) % pr_shepherd_count) + 1`.
- Worker loops open or update PRs, but the PR shepherd is responsible for merges and cross-branch CI cleanup.
- After a PR is merged or closed, the worker or shepherd loop should clean up that branch in its dedicated worktree and return to the default-branch base before picking new work.
- Scout, reviewer, worker, feature-requester, and PR-shepherd runs should use bounded subagents for reconnaissance or disjoint sidecar work whenever the runtime supports delegation.
- `swarm.sh run` is the endless supervisor mode. It keeps the selected role loops running and restarts them when they exit.
- `swarm-launchd.sh install` installs a macOS LaunchAgent that runs `swarm.sh run` at login with `KeepAlive`, which is the current stopgap until Hivemind owns this scheduling natively.
- `swarm.sh logs --follow` tails the active log files as one stream and color-codes each line by agent role.
- `scout-*` is the only top-level swarm role allowed to use the Codex browser tool.

## Useful env overrides

- `HIVEMIND_SWARM_WORKTREE_ROOT`: override the base directory for loop worktrees.
- `HIVEMIND_SWARM_RUNTIME_ROOT`: override where logs and pid files are stored.
- `HIVEMIND_SWARM_SUPERVISOR_SLEEP_SECONDS`
- `HIVEMIND_SWARM_DEFAULT_REVIEWERS`
- `HIVEMIND_SWARM_DEFAULT_WORKERS`
- `HIVEMIND_SWARM_DEFAULT_FEATURE_REQUESTERS`
- `HIVEMIND_SWARM_DEFAULT_SCOUTS`
- `HIVEMIND_SWARM_DEFAULT_PR_SHEPHERDS`
- `HIVEMIND_SCOUT_SLEEP_SECONDS`
- `HIVEMIND_REVIEWER_SLEEP_SECONDS`
- `HIVEMIND_WORKER_SLEEP_SECONDS`
- `HIVEMIND_FEATURE_REQUESTER_SLEEP_SECONDS`
- `HIVEMIND_PR_SHEPHERD_SLEEP_SECONDS`
- `HIVEMIND_SCOUT_MAX_RUNS`
- `HIVEMIND_REVIEWER_MAX_RUNS`
- `HIVEMIND_WORKER_MAX_RUNS`
- `HIVEMIND_FEATURE_REQUESTER_MAX_RUNS`
- `HIVEMIND_PR_SHEPHERD_MAX_RUNS`
