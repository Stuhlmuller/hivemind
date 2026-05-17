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

export HIVEMIND_SCOUT_SLEEP_SECONDS="${HIVEMIND_SCOUT_SLEEP_SECONDS:-${HIVEMIND_BROWSER_USER_SLEEP_SECONDS:-10800}}"
export HIVEMIND_SCOUT_MAX_RUNS="${HIVEMIND_SCOUT_MAX_RUNS:-${HIVEMIND_BROWSER_USER_MAX_RUNS:-0}}"

exec "$script_dir/scout-loop.sh" "$run_root" "$@"
