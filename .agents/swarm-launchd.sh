#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$script_dir/loop-common.sh"
repo_root="${repo_root:-}"

label="${HIVEMIND_SWARM_LAUNCHD_LABEL:-dev.hivemind.github-swarm}"
launch_agents_dir="${HOME}/Library/LaunchAgents"
plist_path="$launch_agents_dir/$label.plist"
launchctl_domain="gui/$(id -u)"
swarm_script="$script_dir/swarm.sh"

usage() {
  cat <<'EOF'
usage: .agents/swarm-launchd.sh <print-plist|install|uninstall|status|paths> [role...]

Commands:
  print-plist  Render the LaunchAgent plist for the requested roles
  install      Install and start the LaunchAgent for the requested roles
  uninstall    Stop and remove the LaunchAgent
  status       Print the current LaunchAgent status
  paths        Show the plist, runtime, and worktree paths used by the LaunchAgent
EOF
}

ensure_macos() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    return
  fi

  echo "[swarm-launchd] LaunchAgent automation is only supported on macOS" >&2
  exit 1
}

platform_state_root() {
  printf '%s\n' "${HOME}/Library/Application Support/Hivemind"
}

launchd_state_root() {
  local repo_name

  repo_name="$(basename "$repo_root")"
  printf '%s\n' "${HIVEMIND_SWARM_LAUNCHD_STATE_ROOT:-$(platform_state_root)/swarm/$repo_name}"
}

launchd_runtime_root() {
  printf '%s\n' "${HIVEMIND_SWARM_RUNTIME_ROOT:-$(launchd_state_root)/runtime}"
}

launchd_worktree_root() {
  printf '%s\n' "${HIVEMIND_SWARM_WORKTREE_ROOT:-$(launchd_state_root)/worktrees}"
}

launchd_stdout_path() {
  printf '%s\n' "$(launchd_runtime_root)/launchd.stdout.log"
}

launchd_stderr_path() {
  printf '%s\n' "$(launchd_runtime_root)/launchd.stderr.log"
}

xml_escape() {
  sed \
    -e 's/&/\&amp;/g' \
    -e 's/</\&lt;/g' \
    -e 's/>/\&gt;/g' \
    -e 's/"/\&quot;/g' \
    -e "s/'/\&apos;/g"
}

escaped() {
  printf '%s' "$1" | xml_escape
}

role_argument_lines() {
  local role

  for role in "$@"; do
    printf '    <string>%s</string>\n' "$(escaped "$role")"
  done
}

print_plist() {
  local runtime_root
  local worktree_root
  local stdout_path
  local stderr_path
  local launchd_path

  runtime_root="$(launchd_runtime_root)"
  worktree_root="$(launchd_worktree_root)"
  stdout_path="$(launchd_stdout_path)"
  stderr_path="$(launchd_stderr_path)"
  launchd_path="${HIVEMIND_SWARM_LAUNCHD_PATH:-${PATH:-/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}}"

  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$(escaped "$label")</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(escaped "$swarm_script")</string>
    <string>run</string>
$(role_argument_lines "$@")
  </array>
  <key>WorkingDirectory</key>
  <string>$(escaped "$repo_root")</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(escaped "$launchd_path")</string>
    <key>HIVEMIND_SWARM_RUNTIME_ROOT</key>
    <string>$(escaped "$runtime_root")</string>
    <key>HIVEMIND_SWARM_WORKTREE_ROOT</key>
    <string>$(escaped "$worktree_root")</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$(escaped "$stdout_path")</string>
  <key>StandardErrorPath</key>
  <string>$(escaped "$stderr_path")</string>
</dict>
</plist>
EOF
}

install_launchd() {
  ensure_macos
  ensure_not_nested_codex "swarm-launchd"
  mkdir -p "$launch_agents_dir" "$(launchd_runtime_root)" "$(launchd_worktree_root)"
  print_plist "$@" >"$plist_path"
  launchctl bootout "$launchctl_domain" "$plist_path" >/dev/null 2>&1 || true
  launchctl bootstrap "$launchctl_domain" "$plist_path"
  launchctl kickstart -k "$launchctl_domain/$label"
  echo "[swarm-launchd] installed $label"
  echo "[swarm-launchd] plist: $plist_path"
}

uninstall_launchd() {
  ensure_macos

  if [[ -f "$plist_path" ]]; then
    launchctl bootout "$launchctl_domain" "$plist_path" >/dev/null 2>&1 || true
    rm -f "$plist_path"
  fi

  echo "[swarm-launchd] removed $label"
}

status_launchd() {
  ensure_macos
  launchctl print "$launchctl_domain/$label"
}

show_paths() {
  echo "label: $label"
  echo "plist: $plist_path"
  echo "runtime_root: $(launchd_runtime_root)"
  echo "worktree_root: $(launchd_worktree_root)"
  echo "stdout_log: $(launchd_stdout_path)"
  echo "stderr_log: $(launchd_stderr_path)"
}

main() {
  local command="${1:-}"
  shift || true

  case "$command" in
    print-plist)
      ensure_macos
      print_plist "$@"
      ;;
    install)
      install_launchd "$@"
      ;;
    uninstall)
      uninstall_launchd
      ;;
    status)
      status_launchd
      ;;
    paths)
      show_paths
      ;;
    *)
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
