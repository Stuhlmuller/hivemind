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

worktree_root_default="${TMPDIR:-/tmp}/hivemind-swarm-worktrees/$(basename "$repo_root")"
worktree_root="${HIVEMIND_SWARM_WORKTREE_ROOT:-$worktree_root_default}"

roles=(scout worker-a worker-b pr-shepherd)

usage() {
  cat <<'EOF'
usage: .agents/swarm.sh <start|run|status|logs|stop> [role...]

Commands:
  start   Provision dedicated worktrees and launch the requested loops
  run     Supervise the requested loops forever and restart them when they exit
  status  Show role status, worktree path, branch, and latest log line
  logs    Show recent logs or follow them with colorized role prefixes
  stop    Stop the requested loops

Roles:
  scout
  worker-a
  worker-b
  pr-shepherd
EOF
}

role_script() {
  case "$1" in
    scout) printf '%s\n' "$script_dir/scout-loop.sh" ;;
    worker-a) printf '%s\n' "$script_dir/worker-loop-a.sh" ;;
    worker-b) printf '%s\n' "$script_dir/worker-loop-b.sh" ;;
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

selected_roles() {
  if [[ "$#" -eq 0 ]]; then
    printf '%s\n' "${roles[@]}"
    return
  fi

  printf '%s\n' "$@"
}

role_color_code() {
  case "$1" in
    scout) printf '%s\n' "36" ;;
    worker-a) printf '%s\n' "32" ;;
    worker-b) printf '%s\n' "35" ;;
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

  cleanup_stale_pid "$pid_file"
  if [[ -f "$pid_file" ]]; then
    pid="$(<"$pid_file")"
    echo "[swarm] $role is already running with pid $pid"
    return
  fi

  ensure_detached_worktree "$run_root"
  : >"$log_path"

  nohup "$(role_script "$role")" "$run_root" >>"$log_path" 2>&1 &
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
  local -a target_roles=()
  local -a log_paths=()

  while IFS= read -r role; do
    target_roles+=("$role")
    log_path="$(role_log_path "$role")"
    : >>"$log_path"
    log_paths+=("$log_path")
  done < <(selected_roles "$@")

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
      -f|--follow)
        follow_logs_mode="1"
        ;;
      *)
        target_roles+=("$arg")
        ;;
    esac
  done

  if [[ "$follow_logs_mode" == "1" ]]; then
    follow_logs "${target_roles[@]}"
    return
  fi

  while IFS= read -r role; do
    show_logs "$role"
  done < <(selected_roles "${target_roles[@]}")
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

  case "$command" in
    start)
      ensure_not_nested_codex "swarm"
      ensure_git_ready "swarm"
      ensure_codex_ready "swarm"
      ensure_bootstrap_files "swarm"
      ensure_github_ready "swarm"
      while IFS= read -r role; do
        start_role "$role"
      done < <(selected_roles "$@")
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
      while IFS= read -r role; do
        print_status "$role"
      done < <(selected_roles "$@")
      ;;
    logs)
      show_logs_command "$@"
      ;;
    stop)
      while IFS= read -r role; do
        stop_role "$role"
      done < <(selected_roles "$@")
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
