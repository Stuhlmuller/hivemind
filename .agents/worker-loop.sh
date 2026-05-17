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

loop_label="${HIVEMIND_LOOP_LABEL:-worker}"
slot_index="${HIVEMIND_WORKER_SLOT_INDEX:-${HIVEMIND_DEVELOPER_SLOT_INDEX:-1}}"
slot_count="${HIVEMIND_WORKER_SLOT_COUNT:-${HIVEMIND_DEVELOPER_SLOT_COUNT:-1}}"

export HIVEMIND_LOOP_LABEL="$loop_label"
export HIVEMIND_LOOP_SLEEP_SECONDS="${HIVEMIND_WORKER_SLEEP_SECONDS:-${HIVEMIND_DEVELOPER_SLEEP_SECONDS:-300}}"
export HIVEMIND_LOOP_MAX_RUNS="${HIVEMIND_WORKER_MAX_RUNS:-${HIVEMIND_DEVELOPER_MAX_RUNS:-0}}"
export HIVEMIND_LOOP_REVIEW_PROMPT="${HIVEMIND_WORKER_REVIEW_PROMPT:-${HIVEMIND_DEVELOPER_REVIEW_PROMPT:-Review the current uncommitted changes. Prioritize correctness bugs, regressions, and missing tests before the PR is updated.}}"

if [[ -z "${HIVEMIND_LOOP_PROMPT_PREAMBLE:-}" ]]; then
  loop_prompt_preamble="$(default_loop_prompt_preamble worker "$loop_label" "$slot_index" "$slot_count")"
  export HIVEMIND_LOOP_PROMPT_PREAMBLE="$loop_prompt_preamble"
fi

exec "$script_dir/role-loop.sh" "$run_root" "$script_dir/PROMPT-worker.md" "$@"
