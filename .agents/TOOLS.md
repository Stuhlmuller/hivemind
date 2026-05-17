# Agent Tool Manifest

This file must exist on every new agent spawn. Update it before or immediately after using a new CLI so the repository bootstrap stays current.

## Required toolchain

| Tool | Nix package | Purpose |
| --- | --- | --- |
| `bash` | `bash` | Run repository bootstrap scripts such as `.agents/ralph.sh` |
| `codex` | `external` | Run the Codex agent and `codex review` loop from `.agents/ralph.sh` |
| `gh` | `gh` | Inspect and create GitHub issues from the repository |
| `git` | `git` | Inspect repository state and history |
| `nix` | `external` | Evaluate `flake.nix` and verify whether the repo dev shell is usable in the local environment |
| `open` | `external` | Launch a local browser tab for manual UI verification on macOS |
| `python` | `external` | Run the project virtualenv for local verification and browser smoke checks |
| `pytest` | `external` | Run targeted regression tests from the project virtualenv |
| `qlty` | `external` | Run the required code-quality checks before finishing repository changes |
| `rg` | `ripgrep` | Search the codebase quickly |
| `find` | `findutils` | Discover files and repo structure |
| `sed` | `gnused` | Read targeted file sections |
| `ls` / `pwd` / `cp` | `coreutils` | Basic shell navigation, inspection, and local repo-state setup |
| `uvicorn` | `external` | Serve the local app from the project virtualenv for browser verification |

## Update rule

If any agent uses another CLI, add it here and add the matching package to `flake.nix` in the same change when the tool is available through nix. If the flake cannot be updated or the tool is managed outside nix, record it here with `external` in the package column.
