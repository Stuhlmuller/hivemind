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

if ! command -v codex >/dev/null 2>&1; then
  echo "[ralph] codex is required in PATH" >&2
  exit 1
fi

if [[ ! -f "$prompt_file" ]]; then
  echo "[ralph] missing prompt file: $prompt_file" >&2
  exit 1
fi

if [[ ! -f "$tools_file" ]]; then
  cat >"$tools_file" <<'EOF'
# Agent Tool Manifest

Recreate or update this file before starting a new agent spawn.
EOF
fi

if [[ ! -f "$flake_file" ]]; then
  echo "[ralph] missing flake file: $flake_file" >&2
  exit 1
fi

while :; do
  echo "[ralph] starting Codex run $iteration"
  prompt_text="$(<"$prompt_file")"
  codex "$@" --full-auto -C "$repo_root" "$prompt_text"

  echo "[ralph] starting auto-review for run $iteration"
  codex review --uncommitted "$review_prompt"

  if [[ "$max_runs" -gt 0 && "$iteration" -ge "$max_runs" ]]; then
    echo "[ralph] reached RALPH_MAX_RUNS=$max_runs"
    break
  fi

  iteration=$((iteration + 1))

  if [[ "$sleep_seconds" != "0" ]]; then
    sleep "$sleep_seconds"
  fi
done
