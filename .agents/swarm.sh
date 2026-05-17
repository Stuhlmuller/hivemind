#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/loop-common.sh"
repo_root="${repo_root:-}"

runtime_root_default="$script_dir/runtime/swarm"
runtime_root="${HIVEMIND_SWARM_RUNTIME_ROOT:-$runtime_root_default}"
logs_root="$runtime_root/logs"
pids_root="$runtime_root/pids"
fleet_config_path="$runtime_root/fleet-config.env"

worktree_root_default="${TMPDIR:-/tmp}/hivemind-swarm-worktrees/$(basename "$repo_root")"
worktree_root="${HIVEMIND_SWARM_WORKTREE_ROOT:-$worktree_root_default}"

fleet_reviewers=""
fleet_developers=""
fleet_feature_requesters=""
fleet_browser_users=""
fleet_pr_shepherds=""
config_was_overridden="0"
parsed_non_count_args=()

usage() {
  cat <<EOF
usage: .agents/swarm.sh <start|run|status|logs|stop> [fleet-flags...] [role...]

Commands:
  start   Provision dedicated worktrees and launch the requested fleet or roles
  run     Supervise the requested fleet or roles forever and restart them when they exit
  status  Show role status, worktree path, branch, and latest log line
  logs    Show recent logs or follow them with colorized role prefixes
  stop    Stop the requested fleet or roles

Fleet flags:
  --reviewers <count>           Default: ${HIVEMIND_SWARM_DEFAULT_REVIEWERS:-3}
  --workers <count>             Default: ${HIVEMIND_SWARM_DEFAULT_WORKERS:-${HIVEMIND_SWARM_DEFAULT_DEVELOPERS:-10}}
  --developers <count>          Alias for --workers
  --feature-requesters <count>  Default: ${HIVEMIND_SWARM_DEFAULT_FEATURE_REQUESTERS:-3}
  --scouts <count>              Default: ${HIVEMIND_SWARM_DEFAULT_SCOUTS:-${HIVEMIND_SWARM_DEFAULT_BROWSER_USERS:-1}}
  --browser-users <count>       Alias for --scouts
  --users <count>               Alias for --scouts
  --pr-shepherds <count>        Default: ${HIVEMIND_SWARM_DEFAULT_PR_SHEPHERDS:-1}

Roles:
  reviewer-<n>
  worker-<n>
  feature-requester-<n>
  scout-<n>
  pr-shepherd-<n>

Legacy aliases:
  browser-user-1 -> scout-1
  developer-1 -> worker-1
  developer-2 -> worker-2
  worker-a -> worker-1
  worker-b -> worker-2
  pr-shepherd -> pr-shepherd-1
EOF
}

set_default_fleet_counts() {
  fleet_reviewers="${HIVEMIND_SWARM_DEFAULT_REVIEWERS:-3}"
  fleet_developers="${HIVEMIND_SWARM_DEFAULT_WORKERS:-${HIVEMIND_SWARM_DEFAULT_DEVELOPERS:-10}}"
  fleet_feature_requesters="${HIVEMIND_SWARM_DEFAULT_FEATURE_REQUESTERS:-3}"
  fleet_browser_users="${HIVEMIND_SWARM_DEFAULT_SCOUTS:-${HIVEMIND_SWARM_DEFAULT_BROWSER_USERS:-1}}"
  fleet_pr_shepherds="${HIVEMIND_SWARM_DEFAULT_PR_SHEPHERDS:-1}"
}

validate_count() {
  local value="$1"
  local label="$2"

  if [[ "$value" =~ ^[0-9]+$ ]]; then
    return
  fi

  echo "[swarm] invalid $label count: $value" >&2
  exit 1
}

validate_fleet_counts() {
  validate_count "$fleet_reviewers" "reviewers"
  validate_count "$fleet_developers" "workers"
  validate_count "$fleet_feature_requesters" "feature-requesters"
  validate_count "$fleet_browser_users" "scouts"
  validate_count "$fleet_pr_shepherds" "pr-shepherds"
}

load_fleet_config() {
  set_default_fleet_counts

  if [[ -f "$fleet_config_path" ]]; then
    # shellcheck disable=SC1090
    source "$fleet_config_path"
  fi

  validate_fleet_counts
}

save_fleet_config() {
  cat >"$fleet_config_path" <<EOF
fleet_reviewers=$fleet_reviewers
fleet_developers=$fleet_developers
fleet_feature_requesters=$fleet_feature_requesters
fleet_browser_users=$fleet_browser_users
fleet_pr_shepherds=$fleet_pr_shepherds
EOF
}

parse_count_flags() {
  parsed_non_count_args=()
  config_was_overridden="0"

  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --reviewers)
        [[ "$#" -ge 2 ]] || { echo "[swarm] missing value for --reviewers" >&2; exit 1; }
        fleet_reviewers="$2"
        config_was_overridden="1"
        shift 2
        ;;
      --workers|--developers)
        [[ "$#" -ge 2 ]] || { echo "[swarm] missing value for $1" >&2; exit 1; }
        fleet_developers="$2"
        config_was_overridden="1"
        shift 2
        ;;
      --feature-requesters)
        [[ "$#" -ge 2 ]] || { echo "[swarm] missing value for --feature-requesters" >&2; exit 1; }
        fleet_feature_requesters="$2"
        config_was_overridden="1"
        shift 2
        ;;
      --scouts|--browser-users|--users)
        [[ "$#" -ge 2 ]] || { echo "[swarm] missing value for $1" >&2; exit 1; }
        fleet_browser_users="$2"
        config_was_overridden="1"
        shift 2
        ;;
      --pr-shepherds)
        [[ "$#" -ge 2 ]] || { echo "[swarm] missing value for --pr-shepherds" >&2; exit 1; }
        fleet_pr_shepherds="$2"
        config_was_overridden="1"
        shift 2
        ;;
      --)
        shift
        while [[ "$#" -gt 0 ]]; do
          parsed_non_count_args+=("$1")
          shift
        done
        ;;
      *)
        parsed_non_count_args+=("$1")
        shift
        ;;
    esac
  done

  validate_fleet_counts
}

role_kind() {
  case "$1" in
    scout|scout-1|scout-[1-9]*|browser-user-1|browser-user-[1-9]*)
      printf '%s\n' "scout"
      ;;
    worker-a|worker-b|worker-1|worker-[1-9]*|developer-1|developer-[1-9]*)
      printf '%s\n' "worker"
      ;;
    reviewer-1|reviewer-[1-9]*)
      printf '%s\n' "reviewer"
      ;;
    feature-requester-1|feature-requester-[1-9]*)
      printf '%s\n' "feature-requester"
      ;;
    pr-shepherd|pr-shepherd-1|pr-shepherd-[1-9]*)
      printf '%s\n' "pr-shepherd"
      ;;
    *)
      echo "[swarm] unknown role: $1" >&2
      exit 1
      ;;
  esac
}

role_index() {
  case "$1" in
    scout|worker-a|pr-shepherd)
      printf '%s\n' "1"
      ;;
    worker-b)
      printf '%s\n' "2"
      ;;
    reviewer-*|worker-*|developer-*|feature-requester-*|scout-*|browser-user-*|pr-shepherd-*)
      printf '%s\n' "${1##*-}"
      ;;
    *)
      echo "[swarm] unknown role index for: $1" >&2
      exit 1
      ;;
  esac
}

canonical_role_name() {
  case "$1" in
    scout) printf '%s\n' "scout-1" ;;
    browser-user-[1-9]*) printf 'scout-%s\n' "${1##*-}" ;;
    worker-a) printf '%s\n' "worker-1" ;;
    worker-b) printf '%s\n' "worker-2" ;;
    developer-[1-9]*) printf 'worker-%s\n' "${1##*-}" ;;
    pr-shepherd) printf '%s\n' "pr-shepherd-1" ;;
    reviewer-[1-9]*|worker-[1-9]*|feature-requester-[1-9]*|scout-[1-9]*|pr-shepherd-[1-9]*)
      printf '%s\n' "$1"
      ;;
    *)
      echo "[swarm] unknown role: $1" >&2
      exit 1
      ;;
  esac
}

count_for_role_kind() {
  case "$1" in
    reviewer) printf '%s\n' "$fleet_reviewers" ;;
    worker) printf '%s\n' "$fleet_developers" ;;
    feature-requester) printf '%s\n' "$fleet_feature_requesters" ;;
    scout) printf '%s\n' "$fleet_browser_users" ;;
    pr-shepherd) printf '%s\n' "$fleet_pr_shepherds" ;;
    *)
      echo "[swarm] unknown role kind: $1" >&2
      exit 1
      ;;
  esac
}

validate_role_against_config() {
  local role="$1"
  local kind
  local index
  local max_count

  kind="$(role_kind "$role")"
  index="$(role_index "$role")"
  max_count="$(count_for_role_kind "$kind")"

  if [[ "$index" -le "$max_count" ]]; then
    return
  fi

  echo "[swarm] role $role is outside the configured $kind count of $max_count" >&2
  exit 1
}

emit_numbered_roles() {
  local role_prefix="$1"
  local count="$2"
  local idx

  for ((idx = 1; idx <= count; idx += 1)); do
    printf '%s-%s\n' "$role_prefix" "$idx"
  done
}

generated_roles() {
  emit_numbered_roles "reviewer" "$fleet_reviewers"
  emit_numbered_roles "worker" "$fleet_developers"
  emit_numbered_roles "feature-requester" "$fleet_feature_requesters"
  emit_numbered_roles "scout" "$fleet_browser_users"
  emit_numbered_roles "pr-shepherd" "$fleet_pr_shepherds"
}

selected_roles() {
  local role
  local canonical

  if [[ "$#" -eq 0 ]]; then
    generated_roles
    return
  fi

  for role in "$@"; do
    canonical="$(canonical_role_name "$role")"
    validate_role_against_config "$canonical"
    printf '%s\n' "$canonical"
  done
}

role_script() {
  case "$(role_kind "$1")" in
    reviewer) printf '%s\n' "$script_dir/reviewer-loop.sh" ;;
    worker) printf '%s\n' "$script_dir/worker-loop.sh" ;;
    feature-requester) printf '%s\n' "$script_dir/feature-requester-loop.sh" ;;
    scout) printf '%s\n' "$script_dir/browser-user-loop.sh" ;;
    pr-shepherd) printf '%s\n' "$script_dir/pr-shepherd.sh" ;;
    *)
      echo "[swarm] unknown role: $1" >&2
      exit 1
      ;;
  esac
}

role_worktree() {
  printf '%s\n' "$worktree_root/$1"
}

role_log_path() {
  printf '%s\n' "$logs_root/$1.log"
}

role_pid_path() {
  printf '%s\n' "$pids_root/$1.pid"
}

role_color_code() {
  case "$(role_kind "$1")" in
    scout) printf '%s\n' "36" ;;
    reviewer) printf '%s\n' "34" ;;
    worker) printf '%s\n' "32" ;;
    feature-requester) printf '%s\n' "35" ;;
    pr-shepherd) printf '%s\n' "33" ;;
    *) printf '%s\n' "37" ;;
  esac
}

should_colorize_logs() {
  if [[ "${HIVEMIND_SWARM_FORCE_COLOR:-}" == "1" ]]; then
    return 0
  fi

  if [[ -n "${NO_COLOR:-}" ]]; then
    return 1
  fi

  [[ -t 1 ]]
}

print_role_log_line() {
  local role="$1"
  local line="$2"
  local color_code

  if should_colorize_logs; then
    color_code="$(role_color_code "$role")"
    printf '\033[%sm[%s]\033[0m %s\n' "$color_code" "$role" "$line"
    return
  fi

  printf '[%s] %s\n' "$role" "$line"
}

ensure_runtime_dirs() {
  mkdir -p "$runtime_root" "$logs_root" "$pids_root"
}

role_is_running() {
  local role="$1"
  local pid_file
  local pid=""

  pid_file="$(role_pid_path "$role")"
  cleanup_stale_pid "$pid_file"

  if [[ ! -f "$pid_file" ]]; then
    return 1
  fi

  pid="$(<"$pid_file")"
  pid_is_running "$pid"
}

cleanup_stale_pid() {
  local pid_file="$1"
  local pid=""

  if [[ ! -f "$pid_file" ]]; then
    return
  fi

  pid="$(<"$pid_file")"
  if pid_is_running "$pid"; then
    return
  fi

  rm -f "$pid_file"
}

role_env_args() {
  local role="$1"
  local kind
  local index
  local slot_count

  kind="$(role_kind "$role")"
  index="$(role_index "$role")"
  slot_count="$(count_for_role_kind "$kind")"

  printf '%s\n' "HIVEMIND_LOOP_LABEL=$role"

  case "$kind" in
    reviewer)
      printf '%s\n' "HIVEMIND_REVIEWER_SLOT_INDEX=$index"
      printf '%s\n' "HIVEMIND_REVIEWER_SLOT_COUNT=$slot_count"
      ;;
    worker)
      printf '%s\n' "HIVEMIND_WORKER_SLOT_INDEX=$index"
      printf '%s\n' "HIVEMIND_WORKER_SLOT_COUNT=$slot_count"
      ;;
    feature-requester)
      printf '%s\n' "HIVEMIND_FEATURE_REQUESTER_SLOT_INDEX=$index"
      printf '%s\n' "HIVEMIND_FEATURE_REQUESTER_SLOT_COUNT=$slot_count"
      ;;
    scout)
      printf '%s\n' "HIVEMIND_SCOUT_SLOT_INDEX=$index"
      printf '%s\n' "HIVEMIND_SCOUT_SLOT_COUNT=$slot_count"
      ;;
    pr-shepherd)
      printf '%s\n' "HIVEMIND_PR_SHEPHERD_SLOT_INDEX=$index"
      printf '%s\n' "HIVEMIND_PR_SHEPHERD_SLOT_COUNT=$slot_count"
      ;;
  esac
}

start_role() {
  local role="$1"
  local pid_file
  local log_path
  local run_root
  local pid
  local env_line
  local -a env_args=()

  pid_file="$(role_pid_path "$role")"
  log_path="$(role_log_path "$role")"
  run_root="$(role_worktree "$role")"

  cleanup_stale_pid "$pid_file"
  if [[ -f "$pid_file" ]]; then
    pid="$(<"$pid_file")"
    echo "[swarm] $role is already running with pid $pid"
    return
  fi

  ensure_detached_worktree "$run_root"
  : >"$log_path"

  while IFS= read -r env_line; do
    env_args+=("$env_line")
  done < <(role_env_args "$role")

  nohup env "${env_args[@]}" "$(role_script "$role")" "$run_root" >>"$log_path" 2>&1 &
  pid="$!"
  printf '%s\n' "$pid" >"$pid_file"
  echo "[swarm] started $role (pid $pid) in $run_root"
}

ensure_role_started() {
  local role="$1"

  if role_is_running "$role"; then
    return
  fi

  start_role "$role"
}

stop_role() {
  local role="$1"
  local pid_file
  local pid=""

  pid_file="$(role_pid_path "$role")"
  cleanup_stale_pid "$pid_file"

  if [[ ! -f "$pid_file" ]]; then
    echo "[swarm] $role is not running"
    return
  fi

  pid="$(<"$pid_file")"
  if pid_is_running "$pid"; then
    kill "$pid"
    sleep 1
  fi

  rm -f "$pid_file"
  echo "[swarm] stopped $role"
}

run_supervisor() {
  local supervisor_sleep_seconds="${HIVEMIND_SWARM_SUPERVISOR_SLEEP_SECONDS:-60}"
  local role
  local -a supervisor_roles=()

  while IFS= read -r role; do
    supervisor_roles+=("$role")
  done < <(selected_roles "$@")

  supervisor_cleanup() {
    local cleanup_role

    for cleanup_role in "${supervisor_roles[@]}"; do
      stop_role "$cleanup_role"
    done
  }

  trap 'printf "\n[swarm] stopping supervisor\n"; supervisor_cleanup; exit 0' INT TERM

  echo "[swarm] starting endless supervisor for roles: ${supervisor_roles[*]}"

  while :; do
    for role in "${supervisor_roles[@]}"; do
      ensure_role_started "$role"
    done
    sleep "$supervisor_sleep_seconds"
  done
}

print_status() {
  local role="$1"
  local pid_file
  local log_path
  local run_root
  local status="stopped"
  local pid=""
  local branch="missing"
  local last_line="(no log yet)"

  pid_file="$(role_pid_path "$role")"
  log_path="$(role_log_path "$role")"
  run_root="$(role_worktree "$role")"

  cleanup_stale_pid "$pid_file"

  if [[ -f "$pid_file" ]]; then
    pid="$(<"$pid_file")"
    if pid_is_running "$pid"; then
      status="running"
    else
      status="stopped"
    fi
  fi

  if [[ -d "$run_root" ]]; then
    branch="$(current_branch_display "$run_root")"
  fi

  if [[ -f "$log_path" ]]; then
    last_line="$(tail -n 1 "$log_path" | tr -d '\r')"
    if [[ -z "$last_line" ]]; then
      last_line="(log is empty)"
    fi
  fi

  echo "$role"
  echo "status: $status${pid:+ (pid $pid)}"
  echo "worktree: $run_root"
  echo "branch: $branch"
  echo "log: $log_path"
  echo "last: $last_line"
}

show_logs() {
  local role="$1"
  local log_path

  log_path="$(role_log_path "$role")"
  echo "== $role =="
  if [[ -f "$log_path" ]]; then
    tail -n "${HIVEMIND_SWARM_TAIL_LINES:-40}" "$log_path"
    return
  fi

  echo "(no log yet)"
}

role_from_log_path() {
  basename "$1" .log
}

follow_logs() {
  local follow_max_lines="${HIVEMIND_SWARM_FOLLOW_MAX_LINES:-0}"
  local current_role=""
  local line
  local printed_lines=0
  local role
  local log_path
  local -a log_paths=()

  while IFS= read -r role; do
    log_path="$(role_log_path "$role")"
    : >>"$log_path"
    log_paths+=("$log_path")
  done < <(selected_roles "$@")

  while IFS= read -r line; do
    case "$line" in
      "==> "*" <==" )
        log_path="${line#==> }"
        log_path="${log_path% <==}"
        current_role="$(role_from_log_path "$log_path")"
        ;;
      "" )
        continue
        ;;
      * )
        if [[ -z "$current_role" ]]; then
          continue
        fi
        print_role_log_line "$current_role" "$line"
        printed_lines=$((printed_lines + 1))
        if [[ "$follow_max_lines" -gt 0 && "$printed_lines" -ge "$follow_max_lines" ]]; then
          break
        fi
        ;;
    esac
  done < <(tail -n "${HIVEMIND_SWARM_TAIL_LINES:-40}" -F -v "${log_paths[@]}" 2>/dev/null)
}

show_logs_command() {
  local follow_logs_mode="0"
  local arg
  local role
  local -a target_roles=()

  while [[ "$#" -gt 0 ]]; do
    arg="$1"
    shift
    case "$arg" in
      -f|-F|--follow)
        follow_logs_mode="1"
        ;;
      *)
        target_roles+=("$arg")
        ;;
    esac
  done

  if [[ "$follow_logs_mode" == "1" ]]; then
    if [[ "${#target_roles[@]}" -gt 0 ]]; then
      follow_logs "${target_roles[@]}"
    else
      follow_logs
    fi
    return
  fi

  if [[ "${#target_roles[@]}" -gt 0 ]]; then
    while IFS= read -r role; do
      show_logs "$role"
    done < <(selected_roles "${target_roles[@]}")
    return
  fi

  while IFS= read -r role; do
    show_logs "$role"
  done < <(selected_roles)
}

main() {
  local command="${1:-}"
  shift || true
  local role

  run_selected_roles() {
    if [[ "${#parsed_non_count_args[@]}" -gt 0 ]]; then
      selected_roles "${parsed_non_count_args[@]}"
      return
    fi

    selected_roles
  }

  if [[ -z "$command" ]]; then
    usage >&2
    exit 1
  fi

  ensure_runtime_dirs
  load_fleet_config
  parse_count_flags "$@"

  case "$command" in
    start)
      ensure_not_nested_codex "swarm"
      ensure_git_ready "swarm"
      ensure_codex_ready "swarm"
      ensure_bootstrap_files "swarm"
      ensure_github_ready "swarm"
      if [[ "$config_was_overridden" == "1" || ! -f "$fleet_config_path" ]]; then
        save_fleet_config
      fi
      while IFS= read -r role; do
        start_role "$role"
      done < <(run_selected_roles)
      ;;
    run)
      ensure_not_nested_codex "swarm"
      ensure_git_ready "swarm"
      ensure_codex_ready "swarm"
      ensure_bootstrap_files "swarm"
      ensure_github_ready "swarm"
      if [[ "$config_was_overridden" == "1" || ! -f "$fleet_config_path" ]]; then
        save_fleet_config
      fi
      if [[ "${#parsed_non_count_args[@]}" -gt 0 ]]; then
        run_supervisor "${parsed_non_count_args[@]}"
      else
        run_supervisor
      fi
      ;;
    status)
      ensure_git_ready "swarm"
      while IFS= read -r role; do
        print_status "$role"
      done < <(run_selected_roles)
      ;;
    logs)
      if [[ "${#parsed_non_count_args[@]}" -gt 0 ]]; then
        show_logs_command "${parsed_non_count_args[@]}"
      else
        show_logs_command
      fi
      ;;
    stop)
      while IFS= read -r role; do
        stop_role "$role"
      done < <(run_selected_roles)
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
