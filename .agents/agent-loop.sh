#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/swarm-roles.sh"

role_arg="${1:-}"
run_root="${2:-}"

if [[ -z "$role_arg" || -z "$run_root" ]]; then
  echo "usage: $0 <role> <run-root> [codex-exec-args...]" >&2
  exit 1
fi

role="$(canonical_swarm_role "$role_arg")"
shift 2

prompt_file=""
review_prompt=""

loop_sleep_seconds=""
loop_max_runs=""

prompt_file="$(swarm_role_prompt_path "$script_dir" "$role")"
review_prompt="$(swarm_role_review_prompt "$role")"
loop_sleep_seconds="$(swarm_role_sleep_seconds "$role")"
loop_max_runs="$(swarm_role_max_runs "$role")"

export HIVEMIND_LOOP_LABEL="$role"
export HIVEMIND_LOOP_SLEEP_SECONDS="$loop_sleep_seconds"
export HIVEMIND_LOOP_MAX_RUNS="$loop_max_runs"
unset HIVEMIND_LOOP_PROMPT_PREAMBLE

if [[ -n "$review_prompt" ]]; then
  export HIVEMIND_LOOP_REVIEW_PROMPT="$review_prompt"
else
  unset HIVEMIND_LOOP_REVIEW_PROMPT
fi

exec "$script_dir/role-loop.sh" "$run_root" "$prompt_file" "$@"
