#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/loop-common.sh"
# shellcheck disable=SC1091
source "$script_dir/swarm-roles.sh"
repo_root="${repo_root:-}"

runtime_root_default="$script_dir/runtime/swarm"
runtime_root="${HIVEMIND_SWARM_RUNTIME_ROOT:-$runtime_root_default}"
logs_root="$runtime_root/logs"
pids_root="$runtime_root/pids"

worktree_root_default="${TMPDIR:-/tmp}/hivemind-swarm-worktrees/$(basename "$repo_root")"
worktree_root="${HIVEMIND_SWARM_WORKTREE_ROOT:-$worktree_root_default}"

roles=()
while IFS= read -r role; do
  roles+=("$role")
done < <(swarm_role_names)
selected_roles_result=()

usage() {
  cat <<'EOF'
usage: .agents/swarm.sh <start|run|status|logs|stop> [legacy-fleet-flags...] [role...]

Commands:
  start   Provision dedicated worktrees and launch the requested loops
  run     Supervise the requested loops forever and restart them when they exit
  status  Show role status, worktree path, branch, and latest log line
  logs    Show recent logs or follow them with colorized role prefixes
  stop    Stop the requested loops

Roles:
  reviewer
  worker
  feature-requester
  scout
  beekeeper

Compatibility aliases:
  reviewer-1 -> reviewer
  feature-requester-1 -> feature-requester
  browser-user -> scout
  scout-1 -> scout
  developer -> worker
  worker-1 -> worker
  worker-a -> worker
  worker-b -> worker
  pr-shepherd -> beekeeper

Legacy fleet flags:
  --reviewers N
  --workers N
  --feature-requesters N
  --scouts N
  --pr-shepherds N
EOF
}

swarm_log_line() {
  local level="$1"
  shift
  local message="$*"

  printf '[swarm] %s: %s\n' "$level" "$message"
}

swarm_log_info() {
  swarm_log_line "info" "$@"
}

swarm_log_warn() {
  swarm_log_line "warn" "$@" >&2
}

swarm_log_error() {
  swarm_log_line "error" "$@" >&2
}

swarm_debug_enabled() {
  case "${HIVEMIND_SWARM_DEBUG:-}" in
    1|true|TRUE|yes|YES|on|ON)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

swarm_log_debug() {
  if swarm_debug_enabled; then
    swarm_log_line "debug" "$@" >&2
  fi
}

legacy_flag_role() {
  case "$1" in
    --reviewers) printf '%s\n' "reviewer" ;;
    --workers) printf '%s\n' "worker" ;;
    --feature-requesters) printf '%s\n' "feature-requester" ;;
    --scouts) printf '%s\n' "scout" ;;
    --pr-shepherds|--beekeepers) printf '%s\n' "beekeeper" ;;
    *)
      swarm_log_error "unknown option: $1"
      return 1
      ;;
  esac
}

role_script() {
  case "$1" in
    reviewer) printf '%s\n' "$script_dir/reviewer-loop.sh" ;;
    worker) printf '%s\n' "$script_dir/worker-loop.sh" ;;
    feature-requester) printf '%s\n' "$script_dir/feature-requester-loop.sh" ;;
    scout) printf '%s\n' "$script_dir/scout-loop.sh" ;;
    beekeeper) printf '%s\n' "$script_dir/beekeeper-loop.sh" ;;
    *)
      swarm_log_error "unknown role: $1"
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

selected_roles() {
  local requested_role=""
  local requested_count=""
  local role=""
  local seen_roles=" "

  if [[ "$#" -eq 0 ]]; then
    printf '%s\n' "${roles[@]}"
    return
  fi

  while [[ "$#" -gt 0 ]]; do
    requested_role="$1"
    shift

    case "$requested_role" in
      --reviewers|--workers|--feature-requesters|--scouts|--pr-shepherds|--beekeepers)
        requested_count="${1:-}"
        if [[ -z "$requested_count" ]]; then
          swarm_log_error "missing count for $requested_role"
          exit 1
        fi
        case "$requested_count" in
          ''|*[!0-9]*)
            swarm_log_error "invalid count for $requested_role: $requested_count"
            exit 1
            ;;
        esac
        shift
        if [[ "$requested_count" -eq 0 ]]; then
          continue
        fi
        role="$(legacy_flag_role "$requested_role")" || exit 1
        ;;
      --*)
        swarm_log_error "unknown option: $requested_role"
        exit 1
        ;;
      *)
        role="$(canonical_swarm_role "$requested_role")" || exit 1
        ;;
    esac

    case "$seen_roles" in
      *" $role "*) ;;
      *)
        seen_roles="${seen_roles}${role} "
        printf '%s\n' "$role"
        ;;
    esac
  done
}

load_selected_roles() {
  local selected_output
  local role

  selected_roles_result=()
  selected_output="$(selected_roles "$@")" || return 1

  while IFS= read -r role; do
    if [[ -n "$role" ]]; then
      selected_roles_result+=("$role")
    fi
  done <<<"$selected_output"
}

role_color_code() {
  swarm_role_color_code "$1" 2>/dev/null || printf '%s\n' "37"
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
  swarm_log_debug "ensuring runtime directories: logs=$logs_root pids=$pids_root"
  mkdir -p "$logs_root" "$pids_root"
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

  swarm_log_debug "removing stale pid file: $pid_file"
  rm -f "$pid_file"
}

start_role() {
  local role="$1"
  local pid_file
  local log_path
  local run_root
  local pid

  pid_file="$(role_pid_path "$role")"
  log_path="$(role_log_path "$role")"
  run_root="$(role_worktree "$role")"

  swarm_log_debug "starting role $role with run root $run_root, pid file $pid_file, log $log_path"
  cleanup_stale_pid "$pid_file"
  if [[ -f "$pid_file" ]]; then
    pid="$(<"$pid_file")"
    swarm_log_warn "$role is already running with pid $pid"
    return
  fi

  ensure_detached_worktree "$run_root"
  : >"$log_path"

  nohup "$(role_script "$role")" "$run_root" >>"$log_path" 2>&1 &
  pid="$!"
  printf '%s\n' "$pid" >"$pid_file"
  swarm_log_info "started $role (pid $pid) in $run_root"
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
    swarm_log_warn "$role is not running"
    return
  fi

  pid="$(<"$pid_file")"
  swarm_log_debug "stopping role $role with pid $pid from $pid_file"
  if pid_is_running "$pid"; then
    kill "$pid"
    sleep 1
  fi

  rm -f "$pid_file"
  swarm_log_info "stopped $role"
}

run_supervisor() {
  local supervisor_sleep_seconds="${HIVEMIND_SWARM_SUPERVISOR_SLEEP_SECONDS:-60}"
  local role
  local -a supervisor_roles=()

  load_selected_roles "$@" || exit 1
  supervisor_roles=("${selected_roles_result[@]}")

  supervisor_cleanup() {
    local cleanup_role

    for cleanup_role in "${supervisor_roles[@]}"; do
      stop_role "$cleanup_role"
    done
  }

  trap 'printf "\n"; swarm_log_info "stopping supervisor"; supervisor_cleanup; exit 0' INT TERM

  swarm_log_info "starting endless supervisor for roles: ${supervisor_roles[*]}"

  while :; do
    for role in "${supervisor_roles[@]}"; do
      swarm_log_debug "supervisor ensuring role is running: $role"
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
  local log_path="$1"
  local role

  for role in "${roles[@]}"; do
    if [[ "$(role_log_path "$role")" == "$log_path" ]]; then
      printf '%s\n' "$role"
      return 0
    fi
  done

  return 1
}

follow_logs() {
  local follow_max_lines="${HIVEMIND_SWARM_FOLLOW_MAX_LINES:-0}"
  local current_role=""
  local line
  local printed_lines=0
  local role
  local log_path
  local -a log_paths=()

  load_selected_roles "$@" || exit 1
  for role in "${selected_roles_result[@]}"; do
    log_path="$(role_log_path "$role")"
    swarm_log_debug "following log for $role: $log_path"
    : >>"$log_path"
    log_paths+=("$log_path")
  done

  while IFS= read -r line; do
    case "$line" in
      "==> "*" <==" )
        log_path="${line#==> }"
        log_path="${log_path% <==}"
        current_role="$(role_from_log_path "$log_path" || true)"
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
    load_selected_roles "${target_roles[@]}" || exit 1
    for role in "${selected_roles_result[@]}"; do
      show_logs "$role"
    done
    return
  fi

  load_selected_roles || exit 1
  for role in "${selected_roles_result[@]}"; do
    show_logs "$role"
  done
}

main() {
  local command="${1:-}"
  shift || true
  local role

  if [[ -z "$command" ]]; then
    usage >&2
    exit 1
  fi

  ensure_runtime_dirs
  swarm_log_debug "command: $command; runtime root: $runtime_root; worktree root: $worktree_root"

  case "$command" in
    start)
      ensure_not_nested_codex "swarm"
      ensure_git_ready "swarm"
      ensure_codex_ready "swarm"
      ensure_bootstrap_files "swarm"
      ensure_github_ready "swarm"
      load_selected_roles "$@" || exit 1
      for role in "${selected_roles_result[@]}"; do
        start_role "$role"
      done
      ;;
    run)
      ensure_not_nested_codex "swarm"
      ensure_git_ready "swarm"
      ensure_codex_ready "swarm"
      ensure_bootstrap_files "swarm"
      ensure_github_ready "swarm"
      run_supervisor "$@"
      ;;
    status)
      ensure_git_ready "swarm"
      load_selected_roles "$@" || exit 1
      for role in "${selected_roles_result[@]}"; do
        print_status "$role"
      done
      ;;
    logs)
      show_logs_command "$@"
      ;;
    stop)
      load_selected_roles "$@" || exit 1
      for role in "${selected_roles_result[@]}"; do
        stop_role "$role"
      done
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
