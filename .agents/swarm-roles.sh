#!/usr/bin/env bash

swarm_role_names() {
  printf '%s\n' reviewer worker feature-requester scout beekeeper
}

canonical_swarm_role() {
  case "$1" in
    reviewer|reviewer-[0-9]*)
      printf '%s\n' "reviewer"
      ;;
    worker|worker-[0-9]*|developer|developer-[0-9]*|worker-a|worker-b)
      printf '%s\n' "worker"
      ;;
    feature-requester|feature-requester-[0-9]*)
      printf '%s\n' "feature-requester"
      ;;
    scout|scout-[0-9]*|browser-user|browser-user-[0-9]*)
      printf '%s\n' "scout"
      ;;
    beekeeper|pr-shepherd|pr-shepherd-[0-9]*)
      printf '%s\n' "beekeeper"
      ;;
    *)
      echo "[swarm] unknown role: $1" >&2
      return 1
      ;;
  esac
}

swarm_role_prompt_path() {
  local script_dir="$1"
  local role="$2"

  case "$role" in
    reviewer) printf '%s\n' "$script_dir/PROMPT-reviewer.md" ;;
    worker) printf '%s\n' "$script_dir/PROMPT-worker.md" ;;
    feature-requester) printf '%s\n' "$script_dir/PROMPT-feature-requester.md" ;;
    scout) printf '%s\n' "$script_dir/PROMPT-scout.md" ;;
    beekeeper) printf '%s\n' "$script_dir/PROMPT-beekeeper.md" ;;
    *)
      echo "[swarm] unknown role: $role" >&2
      return 1
      ;;
  esac
}

swarm_role_sleep_seconds() {
  case "$1" in
    reviewer) printf '%s\n' "${HIVEMIND_REVIEWER_SLEEP_SECONDS:-900}" ;;
    worker) printf '%s\n' "${HIVEMIND_WORKER_SLEEP_SECONDS:-300}" ;;
    feature-requester) printf '%s\n' "${HIVEMIND_FEATURE_REQUESTER_SLEEP_SECONDS:-1200}" ;;
    scout) printf '%s\n' "${HIVEMIND_SCOUT_SLEEP_SECONDS:-1800}" ;;
    beekeeper) printf '%s\n' "${HIVEMIND_BEEKEEPER_SLEEP_SECONDS:-240}" ;;
    *)
      echo "[swarm] unknown role: $1" >&2
      return 1
      ;;
  esac
}

swarm_role_max_runs() {
  case "$1" in
    reviewer) printf '%s\n' "${HIVEMIND_REVIEWER_MAX_RUNS:-0}" ;;
    worker) printf '%s\n' "${HIVEMIND_WORKER_MAX_RUNS:-0}" ;;
    feature-requester) printf '%s\n' "${HIVEMIND_FEATURE_REQUESTER_MAX_RUNS:-0}" ;;
    scout) printf '%s\n' "${HIVEMIND_SCOUT_MAX_RUNS:-0}" ;;
    beekeeper) printf '%s\n' "${HIVEMIND_BEEKEEPER_MAX_RUNS:-0}" ;;
    *)
      echo "[swarm] unknown role: $1" >&2
      return 1
      ;;
  esac
}

swarm_role_review_prompt() {
  case "$1" in
    worker)
      printf '%s\n' "${HIVEMIND_WORKER_REVIEW_PROMPT:-Review the current uncommitted changes. Prioritize correctness bugs, regressions, and missing tests before the PR is updated.}"
      ;;
    reviewer|feature-requester|scout|beekeeper)
      printf '%s\n' ""
      ;;
    *)
      echo "[swarm] unknown role: $1" >&2
      return 1
      ;;
  esac
}

swarm_role_color_code() {
  case "$1" in
    reviewer) printf '%s\n' "34" ;;
    worker) printf '%s\n' "32" ;;
    feature-requester) printf '%s\n' "35" ;;
    scout) printf '%s\n' "36" ;;
    beekeeper) printf '%s\n' "33" ;;
    *)
      echo "[swarm] unknown role: $1" >&2
      return 1
      ;;
  esac
}
