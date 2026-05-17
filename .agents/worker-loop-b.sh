#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

run_root="${1:-}"
if [[ -z "$run_root" ]]; then
  echo "usage: $0 <run-root> [codex-exec-args...]" >&2
  exit 1
fi
shift

export HIVEMIND_LOOP_LABEL="worker-b"
export HIVEMIND_LOOP_SLEEP_SECONDS="${HIVEMIND_WORKER_B_SLEEP_SECONDS:-300}"
export HIVEMIND_LOOP_MAX_RUNS="${HIVEMIND_WORKER_B_MAX_RUNS:-0}"
export HIVEMIND_LOOP_REVIEW_PROMPT="${HIVEMIND_WORKER_B_REVIEW_PROMPT:-Review the current uncommitted changes. Prioritize correctness bugs, regressions, and missing tests before the PR is updated.}"

exec "$script_dir/role-loop.sh" "$run_root" "$script_dir/PROMPT-worker-b.md" "$@"
