# Agent Tool Manifest

This file must exist on every new agent spawn. Update it before or immediately after using a new CLI so the repository bootstrap stays current.

## Required toolchain

| Tool | Nix package | Purpose |
| --- | --- | --- |
| `bash` | `bash` | Run repository bootstrap scripts such as `.agents/ralph.sh` and the GitHub swarm loop scripts |
| `codex` | `external` | Run the Codex agent and `codex review` loops from the repository automation wrappers |
| `gh` | `gh` | Inspect GitHub issues and pull requests, create backlog issues, and merge or update PRs from the automation loops |
| `git` | `git` | Inspect repository state and history |
| `nix` | `external` | Evaluate `flake.nix` and verify whether the repo dev shell is usable in the local environment |
| `python` / `python3` / `python3.12` | `python312` | Run the application test suite, skill validation, and repo-local verification commands |
| `pytest` | `python312Packages.pytest` | Execute focused regression coverage for the changed API and store paths |
| `uvicorn` | `python312Packages.uvicorn` | Start the local FastAPI dev server from the repo checkout |
| `qlty` | `external` | Run the required code-quality checks before finishing repository changes |
| `rg` | `ripgrep` | Search the codebase quickly |
| `find` | `findutils` | Discover files and repo structure |
| `sed` | `gnused` | Read targeted file sections |
| `cat` / `chmod` / `cp` / `kill` / `ls` / `mkdir` / `mktemp` / `nohup` / `pwd` / `rm` / `sleep` / `tail` / `tr` / `wc` | `coreutils` | Basic shell utilities for navigation, temp files, background loop management, log inspection, and focused script verification |

## Update rule

If any agent uses another CLI, add it here and add the matching package to `flake.nix` in the same change when the tool is available through nix. If the flake cannot be updated or the tool is managed outside nix, record it here with `external` in the package column.
