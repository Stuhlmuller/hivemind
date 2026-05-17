## Ralph Subagent Fanout

1. After GitHub preflight passes and you have selected exactly one issue, use delegation aggressively if it is available in this Codex runtime.
2. Once you are inside the dedicated `issue-<number>-<slug>` worktree, spawn a first wave of bounded subagents immediately and in parallel. Use as many concurrent subagents as the runtime safely supports when there are concrete disjoint tasks to hand out.
3. Saturate parallel sidecar work before settling into a long local implementation pass. Bias the live mix this way whenever safe parallel slices exist:
   - reviewer lanes should keep up with worker lanes. Maintain at least one active reviewer or regression-check lane for each active write lane.
   - feature-requester lanes should outpace worker lanes. Keep strictly more future-feature-drafting lanes than active write lanes whenever backlog discovery is still producing concrete ideas.
   - QA tester lanes and issue-finder lanes can run opportunistically. They do not need to match worker count and do not block implementation progress.
4. Good first-wave slices include:
   - explorer subagents for repo reconnaissance, affected-code mapping, regression-risk review, and test-gap discovery
   - worker subagents for disjoint write scopes such as tests, docs, isolated modules, or separate API slices
   - feature-requester or issue-finder subagents that audit gaps and draft future GitHub issue bodies without starting implementation for those future issues
   - QA-oriented subagents that probe edge cases, missing verification, and failure handling around the active issue
5. Keep implementation subagents scoped to the current issue branch. Feature-requester and issue-finder subagents may draft future backlog issues, but they must not create a second implementation branch, start coding a second issue, or open a separate PR.
6. Keep the top-level Ralph run responsible for issue selection, worktree creation, final validation synthesis, commits, pushes, PR creation, PR updates, merge decisions, and final decisions about any new backlog issues to file.
7. Give each subagent explicit ownership. Do not spawn multiple write-capable subagents onto the same files or the same patch slice.
8. Do not use the Codex browser tool from Ralph or its subagents. Reserve it for the main-branch scout lane that validates shipped behavior and files new issues.
9. If the current issue does not have safe parallel slices, say so briefly and continue locally instead of inventing busywork.
