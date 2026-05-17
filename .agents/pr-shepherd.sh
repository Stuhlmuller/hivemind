#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export HIVEMIND_BEEKEEPER_SLEEP_SECONDS="${HIVEMIND_BEEKEEPER_SLEEP_SECONDS:-${HIVEMIND_PR_SHEPHERD_SLEEP_SECONDS:-240}}"
export HIVEMIND_BEEKEEPER_MAX_RUNS="${HIVEMIND_BEEKEEPER_MAX_RUNS:-${HIVEMIND_PR_SHEPHERD_MAX_RUNS:-0}}"

exec "$script_dir/beekeeper-loop.sh" "$@"
