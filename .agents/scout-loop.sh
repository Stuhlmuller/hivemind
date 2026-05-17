#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HIVEMIND_LOOP_LABEL="${HIVEMIND_LOOP_LABEL:-scout}"
export HIVEMIND_SCOUT_SLOT_INDEX="${HIVEMIND_SCOUT_SLOT_INDEX:-1}"
export HIVEMIND_SCOUT_SLOT_COUNT="${HIVEMIND_SCOUT_SLOT_COUNT:-1}"
export HIVEMIND_SCOUT_SLEEP_SECONDS="${HIVEMIND_SCOUT_SLEEP_SECONDS:-${HIVEMIND_BROWSER_USER_SLEEP_SECONDS:-1800}}"
export HIVEMIND_SCOUT_MAX_RUNS="${HIVEMIND_SCOUT_MAX_RUNS:-${HIVEMIND_BROWSER_USER_MAX_RUNS:-0}}"

exec "$script_dir/browser-user-loop.sh" "$@"
