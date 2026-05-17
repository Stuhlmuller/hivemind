#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
prompt_file="$script_dir/PROMPT.md"
tools_file="$script_dir/TOOLS.md"
flake_file="$repo_root/flake.nix"

max_runs="${RALPH_MAX_RUNS:-0}"
sleep_seconds="${RALPH_SLEEP_SECONDS:-0}"
review_prompt="${RALPH_REVIEW_PROMPT:-Review the current uncommitted changes. Prioritize correctness bugs, regressions, and missing tests.}"
iteration=1

trap 'printf "\n[ralph] stopping\n"; exit 0' INT TERM

ensure_not_nested_codex() {
  if [[ -z "${CODEX_SANDBOX:-}" ]]; then
    return
  fi

  echo "[ralph] nested Codex runs are not supported inside a Codex sandbox (${CODEX_SANDBOX})." >&2
  echo "[ralph] run .agents/ralph.sh from a normal terminal session instead." >&2
  exit 1
}

ensure_tools_file() {
  if [[ -f "$tools_file" ]]; then
    return
  fi

  cat >"$tools_file" <<'EOF'
# Agent Tool Manifest

This file must exist on every new agent spawn. Update it before or immediately after using a new CLI so the repository bootstrap stays current.
EOF
}

ensure_flake_file() {
  if [[ -f "$flake_file" ]]; then
    return
  fi

  cat >"$flake_file" <<'EOF'
{
  description = "Hivemind agent development shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f system);
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              bash
              coreutils
              findutils
              gh
              git
              gnused
              ripgrep
            ];
          };
        });
    };
}
EOF
}

run_codex_exec() {
  local prompt_text
  local -a cmd

  prompt_text="$(<"$prompt_file")"
  cmd=(codex exec -C "$repo_root" -s workspace-write)

  if [[ "$#" -gt 0 ]]; then
    cmd+=("$@")
  fi

  cmd+=("$prompt_text")
  "${cmd[@]}"
}

run_auto_review() {
  (
    cd "$repo_root"
    # Codex CLI 0.130.0 rejects `--uncommitted` when a custom prompt is present.
    codex review "$review_prompt"
  )
}

if ! command -v codex >/dev/null 2>&1; then
  echo "[ralph] codex is required in PATH" >&2
  exit 1
fi

if [[ ! -f "$prompt_file" ]]; then
  echo "[ralph] missing prompt file: $prompt_file" >&2
  exit 1
fi

ensure_not_nested_codex
ensure_tools_file
ensure_flake_file

while :; do
  echo "[ralph] starting Codex run $iteration"
  run_codex_exec "$@"

  echo "[ralph] starting auto-review for run $iteration"
  run_auto_review

  if [[ "$max_runs" -gt 0 && "$iteration" -ge "$max_runs" ]]; then
    echo "[ralph] reached RALPH_MAX_RUNS=$max_runs"
    break
  fi

  iteration=$((iteration + 1))

  if [[ "$sleep_seconds" != "0" ]]; then
    sleep "$sleep_seconds"
  fi
done
