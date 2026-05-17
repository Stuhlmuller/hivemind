#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/loop-common.sh"

run_root="${1:-}"
if [[ -z "$run_root" ]]; then
  echo "usage: $0 <run-root> [codex-exec-args...]" >&2
  exit 1
fi
shift

loop_label="${HIVEMIND_LOOP_LABEL:-scout}"
slot_index="${HIVEMIND_SCOUT_SLOT_INDEX:-${HIVEMIND_BROWSER_USER_SLOT_INDEX:-1}}"
slot_count="${HIVEMIND_SCOUT_SLOT_COUNT:-${HIVEMIND_BROWSER_USER_SLOT_COUNT:-1}}"

export HIVEMIND_LOOP_LABEL="$loop_label"
export HIVEMIND_LOOP_SLEEP_SECONDS="${HIVEMIND_SCOUT_SLEEP_SECONDS:-${HIVEMIND_BROWSER_USER_SLEEP_SECONDS:-1800}}"
export HIVEMIND_LOOP_MAX_RUNS="${HIVEMIND_SCOUT_MAX_RUNS:-${HIVEMIND_BROWSER_USER_MAX_RUNS:-0}}"
unset HIVEMIND_LOOP_REVIEW_PROMPT

if [[ -z "${HIVEMIND_LOOP_PROMPT_PREAMBLE:-}" ]]; then
  loop_prompt_preamble="$(default_loop_prompt_preamble scout "$loop_label" "$slot_index" "$slot_count")"
  export HIVEMIND_LOOP_PROMPT_PREAMBLE="$loop_prompt_preamble"
fi

exec "$script_dir/role-loop.sh" "$run_root" "$script_dir/PROMPT-scout.md" "$@"
