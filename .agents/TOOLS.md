# Agent Tool Manifest

This file must exist on every new agent spawn. Update it before or immediately after using a new CLI so the repository bootstrap stays current.

## Required toolchain

| Tool | Nix package | Purpose |
| --- | --- | --- |
| `bash` | `bash` | Run repository bootstrap scripts such as `.agents/ralph.sh` and the GitHub swarm loop scripts |
| `codex` | `external` | Run the Codex agent and `codex review` loops from the repository automation wrappers |
| `gh` | `gh` | Inspect GitHub issues and pull requests, create backlog issues, and merge or update PRs from the automation loops |
| `git` | `git` | Inspect repository state and history |
| `launchctl` / `plutil` | `external` | Install, inspect, and lint the optional macOS LaunchAgent that keeps the swarm running across laptop sessions |
| `nix` | `external` | Evaluate `flake.nix` and verify whether the repo dev shell is usable in the local environment |
| `qlty` | `external` | Run the required code-quality checks before finishing repository changes |
| `rg` | `ripgrep` | Search the codebase quickly |
| `find` | `findutils` | Discover files and repo structure |
| `sed` | `gnused` | Read targeted file sections |
| `cat` / `chmod` / `cp` / `id` / `kill` / `ls` / `mkdir` / `mktemp` / `nohup` / `pwd` / `rm` / `sleep` / `tail` / `tr` / `uname` / `wc` | `coreutils` | Basic shell utilities for navigation, temp files, background loop management, login-session detection, log inspection, and focused script verification |

## Update rule

If any agent uses another CLI, add it here and add the matching package to `flake.nix` in the same change when the tool is available through nix. If the flake cannot be updated or the tool is managed outside nix, record it here with `external` in the package column.
