#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/loop-common.sh"

runtime_root_default="$script_dir/runtime/swarm"
runtime_root="${HIVEMIND_SWARM_RUNTIME_ROOT:-$runtime_root_default}"
logs_root="$runtime_root/logs"
pids_root="$runtime_root/pids"

worktree_root_default="${TMPDIR:-/tmp}/hivemind-swarm-worktrees/$(basename "$repo_root")"
worktree_root="${HIVEMIND_SWARM_WORKTREE_ROOT:-$worktree_root_default}"

roles=(scout worker-a worker-b pr-shepherd)

usage() {
  cat <<'EOF'
usage: .agents/swarm.sh <start|status|logs|stop> [role...]

Commands:
  start   Provision dedicated worktrees and launch the requested loops
  status  Show role status, worktree path, branch, and latest log line
  logs    Tail recent log output for the requested roles
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

ensure_runtime_dirs() {
  mkdir -p "$logs_root" "$pids_root"
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
    status)
      ensure_git_ready "swarm"
      while IFS= read -r role; do
        print_status "$role"
      done < <(selected_roles "$@")
      ;;
    logs)
      while IFS= read -r role; do
        show_logs "$role"
      done < <(selected_roles "$@")
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
