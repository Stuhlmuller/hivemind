# Agent Tool Manifest

Prefer `nix develop` for repo work. `flake.nix` is the source of truth for the nix-managed toolchain.

## External Or Host-Managed Tools

| Tool | Nix package | Purpose |
| --- | --- | --- |
| `codex` | `external` | Run the Codex agent and `codex review` loops from the repository automation wrappers |
| `nix` | `external` | Evaluate `flake.nix` and verify whether the repo dev shell is usable in the local environment |
| `qlty` | `external` | Run the required code-quality checks before finishing repository changes |
| `launchctl` / `plutil` | `external` | Install, inspect, and lint the optional macOS LaunchAgent that keeps the swarm running across laptop sessions |

Common repo tools such as `bash`, `gh`, `git`, `python`, `pytest`, `uvicorn`, `rg`, `sed`, `find`, and coreutils should come from the dev shell instead of being repeated here.

## Update rule

1. Add new repo CLIs to `flake.nix` first.
2. Add a tool here only when it must stay external or host-managed for the run.
3. If the dev shell is blocked by machine-local Nix issues, record the temporary fallback here.
