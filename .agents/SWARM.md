# GitHub Swarm Agents

The repository's supported GitHub automation suite uses a small set of named
top-level agents backed by loop wrappers. The older numbered lane fleet has
been retired in favor of canonical roles plus lightweight compatibility
aliases.

## Roles

- `reviewer`: audits the repository, open PRs, tests, docs, CI, and release posture, then prefers refining existing issues before opening rare grounded follow-up issues without implementing code.
- `feature-requester`: audits product, operator, docs, and developer-experience gaps, then prefers refining existing backlog issues before opening rare focused feature issues without implementation.
- `worker`: owns one issue branch at a time in its dedicated worktree and ships implementation updates.
- `scout`: audits the repository and shipped default-branch behavior, prefers refining existing issues, and is the only role allowed to use the Codex browser tool.
- `beekeeper`: keeps the PR queue healthy by fixing clear CI failures on idle PR branches and merging ready work.
- Every loop prepends a shared subagent delegation policy, and each top-level role may fan out bounded subagents whenever useful while staying responsible for final GitHub mutations.

## Compatibility aliases

- `reviewer-1` maps to `reviewer`.
- `feature-requester-1` maps to `feature-requester`.
- `browser-user` and `scout-1` map to `scout`.
- `developer`, `worker-1`, `worker-a`, and `worker-b` map to `worker`.
- `pr-shepherd` and `pr-shepherd-1` map to `beekeeper`.
- Legacy fleet flags such as `--workers 3 --scouts 1` are still accepted as compatibility inputs. Counts now only decide whether the canonical role is enabled at all; they no longer create separate numbered lanes.

## Start and monitor

```bash
.agents/swarm.sh start
.agents/swarm.sh start reviewer worker scout
.agents/swarm.sh run
.agents/swarm.sh run reviewer worker beekeeper
.agents/swarm.sh status
.agents/swarm.sh logs worker
.agents/swarm.sh logs --follow
.agents/swarm.sh logs --follow worker beekeeper
.agents/swarm.sh stop
```

For "open laptop and let it keep going" development on macOS:

```bash
.agents/swarm-launchd.sh install
.agents/swarm-launchd.sh install worker beekeeper
.agents/swarm-launchd.sh status
```

## Defaults

- Worktrees live under `${TMPDIR:-/tmp}/hivemind-swarm-worktrees/<repo-name>/`.
- Logs and pid files live under `.agents/runtime/swarm/`.
- The official top-level roles are `reviewer`, `feature-requester`, `worker`, `scout`, and `beekeeper`.
- Legacy names are kept only as compatibility aliases for older commands or launchd configurations.
- Worker loops open or update PRs, but the beekeeper is responsible for merges and cross-branch CI cleanup.
- After a PR is merged or closed, the worker or beekeeper loop should clean up that branch in its dedicated worktree and return to the default-branch base before picking new work.
- Reviewer, feature-requester, worker, scout, and beekeeper runs should use bounded subagents aggressively for reconnaissance or disjoint sidecar work whenever the runtime supports delegation.
- `swarm.sh run` is the endless supervisor mode. It keeps the selected role loops running and restarts them when they exit.
- `swarm-launchd.sh install` installs a macOS LaunchAgent that runs `swarm.sh run` at login with `KeepAlive`, which is the current stopgap until Hivemind owns this scheduling natively.
- `swarm.sh logs --follow` tails the active log files as one stream and color-codes each line by agent role.
- `scout` is the only top-level swarm role allowed to use the Codex browser tool.
- Issue-making roles are intentionally conservative: reviewer, feature-requester, and scout runs should open at most one issue per run, and only when existing issues or PRs do not already cover the finding.

## Useful env overrides

- `HIVEMIND_SWARM_WORKTREE_ROOT`: override the base directory for loop worktrees.
- `HIVEMIND_SWARM_RUNTIME_ROOT`: override where logs and pid files are stored.
- `HIVEMIND_SWARM_SUPERVISOR_SLEEP_SECONDS`
- `HIVEMIND_REVIEWER_SLEEP_SECONDS`
- `HIVEMIND_WORKER_SLEEP_SECONDS`
- `HIVEMIND_FEATURE_REQUESTER_SLEEP_SECONDS`
- `HIVEMIND_SCOUT_SLEEP_SECONDS`
- `HIVEMIND_BEEKEEPER_SLEEP_SECONDS`
- `HIVEMIND_REVIEWER_MAX_RUNS`
- `HIVEMIND_WORKER_MAX_RUNS`
- `HIVEMIND_FEATURE_REQUESTER_MAX_RUNS`
- `HIVEMIND_SCOUT_MAX_RUNS`
- `HIVEMIND_BEEKEEPER_MAX_RUNS`

Default issue-maker cadence is intentionally slow: reviewer every 3600 seconds,
feature-requester every 7200 seconds, and scout every 10800 seconds. Override
those values only when the operator explicitly wants faster issue creation.
