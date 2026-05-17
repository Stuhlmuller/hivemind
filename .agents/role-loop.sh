#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/loop-common.sh"
repo_root="${repo_root:-}"

if [[ "$#" -lt 2 ]]; then
  echo "usage: $0 <run-root> <prompt-file> [codex-exec-args...]" >&2
  exit 1
fi

run_root="$1"
prompt_file="$2"
shift 2

loop_label="${HIVEMIND_LOOP_LABEL:-loop}"
sleep_seconds="${HIVEMIND_LOOP_SLEEP_SECONDS:-0}"
max_runs="${HIVEMIND_LOOP_MAX_RUNS:-0}"
review_prompt="${HIVEMIND_LOOP_REVIEW_PROMPT:-}"
iteration=1
shared_prompt_file="$script_dir/PROMPT-subagents.md"

trap 'printf "\n[%s] stopping\n" "$loop_label"; exit 0' INT TERM

if [[ ! -d "$run_root" ]]; then
  echo "[$loop_label] missing run root: $run_root" >&2
  exit 1
fi

if [[ ! -f "$prompt_file" ]]; then
  echo "[$loop_label] missing prompt file: $prompt_file" >&2
  exit 1
fi

if [[ ! -f "$shared_prompt_file" ]]; then
  echo "[$loop_label] missing shared prompt file: $shared_prompt_file" >&2
  exit 1
fi

ensure_not_nested_codex "$loop_label"
ensure_git_ready "$loop_label"
ensure_codex_ready "$loop_label"
ensure_bootstrap_files "$loop_label"
ensure_github_ready "$loop_label"

while :; do
  ensure_github_ready "$loop_label"
  echo "[$loop_label] starting Codex run $iteration in $run_root"
  run_codex_exec "$run_root" "$prompt_file" "$shared_prompt_file" "$@"

  if [[ -n "$review_prompt" ]]; then
    echo "[$loop_label] starting auto-review for run $iteration in $run_root"
    run_codex_review "$run_root" "$review_prompt"
  fi

  if [[ "$max_runs" -gt 0 && "$iteration" -ge "$max_runs" ]]; then
    echo "[$loop_label] reached HIVEMIND_LOOP_MAX_RUNS=$max_runs"
    break
  fi

  iteration=$((iteration + 1))

  if [[ "$sleep_seconds" != "0" ]]; then
    sleep "$sleep_seconds"
  fi
done
