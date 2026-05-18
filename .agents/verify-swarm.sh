#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_repo_root="$(cd "$script_dir/.." && pwd)"

canonicalize_path() {
  local target_path="$1"
  (
    cd "$target_path"
    pwd -P
  )
}

copy_agent_files() {
  local repo_root="$1"

  mkdir -p "$repo_root/.agents" "$repo_root/.agents/skills/hivemind-github-swarm-loop/agents"

  cp "$source_repo_root/.agents/loop-common.sh" "$repo_root/.agents/loop-common.sh"
  cp "$source_repo_root/.agents/swarm-roles.sh" "$repo_root/.agents/swarm-roles.sh"
  cp "$source_repo_root/.agents/agent-loop.sh" "$repo_root/.agents/agent-loop.sh"
  cp "$source_repo_root/.agents/role-loop.sh" "$repo_root/.agents/role-loop.sh"
  cp "$source_repo_root/.agents/reviewer-loop.sh" "$repo_root/.agents/reviewer-loop.sh"
  cp "$source_repo_root/.agents/feature-requester-loop.sh" "$repo_root/.agents/feature-requester-loop.sh"
  cp "$source_repo_root/.agents/scout-loop.sh" "$repo_root/.agents/scout-loop.sh"
  cp "$source_repo_root/.agents/worker-loop.sh" "$repo_root/.agents/worker-loop.sh"
  cp "$source_repo_root/.agents/beekeeper-loop.sh" "$repo_root/.agents/beekeeper-loop.sh"
  cp "$source_repo_root/.agents/browser-user-loop.sh" "$repo_root/.agents/browser-user-loop.sh"
  cp "$source_repo_root/.agents/developer-loop.sh" "$repo_root/.agents/developer-loop.sh"
  cp "$source_repo_root/.agents/worker-loop-a.sh" "$repo_root/.agents/worker-loop-a.sh"
  cp "$source_repo_root/.agents/worker-loop-b.sh" "$repo_root/.agents/worker-loop-b.sh"
  cp "$source_repo_root/.agents/pr-shepherd.sh" "$repo_root/.agents/pr-shepherd.sh"
  cp "$source_repo_root/.agents/swarm.sh" "$repo_root/.agents/swarm.sh"
  cp "$source_repo_root/.agents/swarm-launchd.sh" "$repo_root/.agents/swarm-launchd.sh"
  cp "$source_repo_root/.agents/PROMPT-subagents.md" "$repo_root/.agents/PROMPT-subagents.md"
  cp "$source_repo_root/.agents/PROMPT-scout.md" "$repo_root/.agents/PROMPT-scout.md"
  cp "$source_repo_root/.agents/PROMPT-reviewer.md" "$repo_root/.agents/PROMPT-reviewer.md"
  cp "$source_repo_root/.agents/PROMPT-worker.md" "$repo_root/.agents/PROMPT-worker.md"
  cp "$source_repo_root/.agents/PROMPT-feature-requester.md" "$repo_root/.agents/PROMPT-feature-requester.md"
  cp "$source_repo_root/.agents/PROMPT-beekeeper.md" "$repo_root/.agents/PROMPT-beekeeper.md"
  cp "$source_repo_root/.agents/TOOLS.md" "$repo_root/.agents/TOOLS.md"
  cp "$source_repo_root/.agents/SWARM.md" "$repo_root/.agents/SWARM.md"
  cp "$source_repo_root/.agents/skills/hivemind-github-swarm-loop/SKILL.md" "$repo_root/.agents/skills/hivemind-github-swarm-loop/SKILL.md"
  cp "$source_repo_root/.agents/skills/hivemind-github-swarm-loop/agents/openai.yaml" "$repo_root/.agents/skills/hivemind-github-swarm-loop/agents/openai.yaml"
  cp "$source_repo_root/flake.nix" "$repo_root/flake.nix"

  chmod +x \
    "$repo_root/.agents/loop-common.sh" \
    "$repo_root/.agents/swarm-roles.sh" \
    "$repo_root/.agents/agent-loop.sh" \
    "$repo_root/.agents/role-loop.sh" \
    "$repo_root/.agents/reviewer-loop.sh" \
    "$repo_root/.agents/feature-requester-loop.sh" \
    "$repo_root/.agents/scout-loop.sh" \
    "$repo_root/.agents/worker-loop.sh" \
    "$repo_root/.agents/beekeeper-loop.sh" \
    "$repo_root/.agents/browser-user-loop.sh" \
    "$repo_root/.agents/developer-loop.sh" \
    "$repo_root/.agents/worker-loop-a.sh" \
    "$repo_root/.agents/worker-loop-b.sh" \
    "$repo_root/.agents/pr-shepherd.sh" \
    "$repo_root/.agents/swarm.sh" \
    "$repo_root/.agents/swarm-launchd.sh"
}

setup_case_repo() {
  local case_root="$1"
  local repo_root="$case_root/repo"
  local remote_root="$case_root/remote.git"

  git init --bare "$remote_root" >/dev/null
  git clone "$remote_root" "$repo_root" >/dev/null 2>&1

  copy_agent_files "$repo_root"

  (
    cd "$repo_root"
    git config user.name "Swarm Test"
    git config user.email "swarm-test@example.com"
    git config commit.gpgsign false
    printf '# test\n' > README.md
    git add README.md .agents flake.nix
    git commit -m "init" >/dev/null
    git branch -M main
    git push -u origin main >/dev/null
    git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main
  )

  printf '%s\n' "$repo_root"
}

write_stub_gh() {
  local bin_root="$1"

  cat >"$bin_root/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

case "${1:-}" in
  auth)
    if [[ "${2:-}" == "status" ]]; then
      printf 'github.com\n'
      exit 0
    fi
    ;;
  issue)
    if [[ "${2:-}" == "list" ]]; then
      exit 0
    fi
    ;;
  pr)
    if [[ "${2:-}" == "list" ]]; then
      exit 0
    fi
    ;;
esac

echo "unexpected gh invocation: $*" >&2
exit 1
EOF

  chmod +x "$bin_root/gh"
}

write_stub_codex() {
  local bin_root="$1"

  cat >"$bin_root/codex" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

command_name="${1:-}"
shift || true

extract_cd() {
  local arg

  while [[ "$#" -gt 0 ]]; do
    arg="$1"
    shift
    case "$arg" in
      -C|--cd)
        printf '%s\n' "$1"
        return 0
        ;;
    esac
  done

  return 1
}

case "$command_name" in
  exec)
    run_root="$(extract_cd "$@")"
    prompt_text="${@: -1}"
    printf '%s\n' "$run_root" >>"${HIVEMIND_SWARM_CAPTURE_DIR:?missing}/$HIVEMIND_LOOP_LABEL.exec"
    printf '%s\n' "$prompt_text" >>"${HIVEMIND_SWARM_CAPTURE_DIR:?missing}/$HIVEMIND_LOOP_LABEL.prompt"
    exit 0
    ;;
  review)
    pwd >>"${HIVEMIND_SWARM_CAPTURE_DIR:?missing}/$HIVEMIND_LOOP_LABEL.review"
    printf '%s\n' "${1:-}" >>"${HIVEMIND_SWARM_CAPTURE_DIR:?missing}/$HIVEMIND_LOOP_LABEL.review_prompt"
    exit 0
    ;;
esac

echo "unexpected codex invocation: $command_name $*" >&2
exit 1
EOF

  chmod +x "$bin_root/codex"
}

wait_for_file() {
  local file_path="$1"
  local attempts=0

  while [[ ! -f "$file_path" ]]; do
    attempts=$((attempts + 1))
    if [[ "$attempts" -gt 50 ]]; then
      echo "timed out waiting for $file_path" >&2
      exit 1
    fi
    sleep 0.1
  done
}

wait_for_line_count() {
  local file_path="$1"
  local minimum_lines="$2"
  local attempts=0
  local line_count=0

  while :; do
    if [[ -f "$file_path" ]]; then
      line_count="$(wc -l <"$file_path" | tr -d '[:space:]')"
      if [[ "$line_count" -ge "$minimum_lines" ]]; then
        return
      fi
    fi
    attempts=$((attempts + 1))
    if [[ "$attempts" -gt 80 ]]; then
      echo "timed out waiting for $file_path to reach $minimum_lines lines" >&2
      [[ -f "$file_path" ]] && cat "$file_path" >&2
      exit 1
    fi
    sleep 0.1
  done
}

wait_for_process_exit() {
  local pid="$1"
  local watchdog_pid

  (
    sleep 5
    if kill -0 "$pid" >/dev/null 2>&1; then
      echo "timed out waiting for process $pid to exit" >&2
      kill "$pid" >/dev/null 2>&1 || true
    fi
  ) &
  watchdog_pid="$!"

  if wait "$pid"; then
    kill "$watchdog_pid" >/dev/null 2>&1 || true
    wait "$watchdog_pid" >/dev/null 2>&1 || true
    return
  fi

  kill "$watchdog_pid" >/dev/null 2>&1 || true
  wait "$watchdog_pid" >/dev/null 2>&1 || true
  echo "process $pid exited unexpectedly" >&2
  exit 1
}

assert_file_equals() {
  local file_path="$1"
  local expected_path="$2"

  if [[ "$(canonicalize_path "$(tail -n 1 "$file_path")")" == "$(canonicalize_path "$expected_path")" ]]; then
    return
  fi

  echo "unexpected path in $file_path" >&2
  cat "$file_path" >&2
  exit 1
}

assert_prompt_includes_subagent_policy() {
  local file_path="$1"

  if rg -q "## Subagent Delegation" "$file_path"; then
    return
  fi

  echo "missing subagent policy in $file_path" >&2
  cat "$file_path" >&2
  exit 1
}

assert_file_contains() {
  local file_path="$1"
  local needle="$2"

  if grep -F -- "$needle" "$file_path" >/dev/null; then
    return
  fi

  echo "missing expected text in $file_path: $needle" >&2
  cat "$file_path" >&2
  exit 1
}

assert_text_includes() {
  local haystack="$1"
  local needle="$2"

  if [[ "$haystack" == *"$needle"* ]]; then
    return
  fi

  echo "missing expected text: $needle" >&2
  echo "$haystack" >&2
  exit 1
}

swarm_env() {
  env \
    PATH="$bin_root:$PATH" \
    CODEX_SANDBOX="" \
    HOME="$home_root" \
    HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
    HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
    HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
    HIVEMIND_REVIEWER_MAX_RUNS=1 \
    HIVEMIND_REVIEWER_SLEEP_SECONDS=0 \
    HIVEMIND_WORKER_MAX_RUNS=1 \
    HIVEMIND_WORKER_SLEEP_SECONDS=0 \
    HIVEMIND_FEATURE_REQUESTER_MAX_RUNS=1 \
    HIVEMIND_FEATURE_REQUESTER_SLEEP_SECONDS=0 \
    HIVEMIND_SCOUT_MAX_RUNS=1 \
    HIVEMIND_SCOUT_SLEEP_SECONDS=0 \
    HIVEMIND_BEEKEEPER_MAX_RUNS=1 \
    HIVEMIND_BEEKEEPER_SLEEP_SECONDS=0 \
    "$@"
}

case_root="$(mktemp -d "${TMPDIR:-/tmp}/swarm-check.XXXXXX")"
bin_root="$case_root/bin"
capture_root="$case_root/capture"
runtime_root="$case_root/runtime"
worktree_root="$case_root/worktrees"
home_root="$case_root/home"

mkdir -p "$bin_root" "$capture_root" "$home_root"
write_stub_gh "$bin_root"
write_stub_codex "$bin_root"
repo_root="$(setup_case_repo "$case_root")"

debug_status_output="$(
  swarm_env HIVEMIND_SWARM_DEBUG=1 bash "$repo_root/.agents/swarm.sh" status worker 2>&1
)"
assert_text_includes "$debug_status_output" "[swarm] debug: command: status"
assert_text_includes "$debug_status_output" "runtime root: $runtime_root"
assert_text_includes "$debug_status_output" "worktree root: $worktree_root"

stop_missing_output="$(swarm_env bash "$repo_root/.agents/swarm.sh" stop worker 2>&1)"
assert_text_includes "$stop_missing_output" "[swarm] warn: worker is not running"

invalid_status_output=""
if invalid_status_output="$(swarm_env bash "$repo_root/.agents/swarm.sh" status --unknown-role 2>&1)"; then
  echo "expected unknown option to fail" >&2
  echo "$invalid_status_output" >&2
  exit 1
fi
assert_text_includes "$invalid_status_output" "[swarm] error: unknown option: --unknown-role"

start_output="$(swarm_env bash "$repo_root/.agents/swarm.sh" start 2>&1)"
assert_text_includes "$start_output" "[swarm] info: started reviewer (pid "

for role in reviewer worker feature-requester scout beekeeper; do
  wait_for_file "$capture_root/$role.exec"
  wait_for_file "$capture_root/$role.prompt"
done
wait_for_file "$capture_root/worker.review"

assert_file_equals "$capture_root/reviewer.exec" "$worktree_root/reviewer"
assert_file_equals "$capture_root/worker.exec" "$worktree_root/worker"
assert_file_equals "$capture_root/feature-requester.exec" "$worktree_root/feature-requester"
assert_file_equals "$capture_root/scout.exec" "$worktree_root/scout"
assert_file_equals "$capture_root/beekeeper.exec" "$worktree_root/beekeeper"
assert_file_equals "$capture_root/worker.review" "$worktree_root/worker"

for role in reviewer worker feature-requester scout beekeeper; do
  assert_prompt_includes_subagent_policy "$capture_root/$role.prompt"
done

assert_file_contains "$capture_root/scout.prompt" "This is the only loop allowed to use the Codex browser tool."
assert_file_contains "$capture_root/reviewer.prompt" "Do not use the Codex browser tool in this loop."
assert_file_contains "$capture_root/reviewer.prompt" "Audit the full open PR queue oldest-first by \`createdAt\`"
assert_file_contains "$capture_root/reviewer.prompt" "Do not rely on a capped \`gh pr list\` result before enforcing oldest-first ordering."
assert_file_contains "$capture_root/worker.prompt" "Configured priority labels: security, bug, help wanted."
assert_file_contains "$capture_root/worker.prompt" "Leave merging to the beekeeper loop even if the checks are already green."
assert_file_contains "$capture_root/worker.prompt" "Do not use the Codex browser tool. Leave live browser validation to the main-branch scout agent."
assert_file_contains "$capture_root/feature-requester.prompt" "Do not use the Codex browser tool in this loop."
assert_file_contains "$capture_root/beekeeper.prompt" "Do not use the Codex browser tool. Leave live browser validation to the main-branch scout agent."
assert_file_contains "$capture_root/beekeeper.prompt" "Fetch the full open pull request queue with GitHub pagination before sorting."
assert_file_contains "$capture_root/beekeeper.prompt" "Sort the full open PR queue oldest-first by"
assert_file_contains "$capture_root/beekeeper.prompt" "Do not rely on a capped \`gh pr list\` result before enforcing oldest-first ordering."
assert_file_contains "$capture_root/beekeeper.prompt" "Close irrelevant or obsolete PRs completely after confirming there is no active worker ownership."

status_output="$(swarm_env bash "$repo_root/.agents/swarm.sh" status)"
for role in reviewer worker feature-requester scout beekeeper; do
  if [[ "$status_output" != *"$role"* ]]; then
    echo "status output missing $role" >&2
    echo "$status_output" >&2
    exit 1
  fi
done

printf 'scout sample line\n' >>"$runtime_root/logs/scout.log"
printf 'worker sample line\n' >>"$runtime_root/logs/worker.log"

follow_output="$(
  swarm_env \
    HIVEMIND_SWARM_TAIL_LINES=1 \
    HIVEMIND_SWARM_FOLLOW_MAX_LINES=2 \
    HIVEMIND_SWARM_FORCE_COLOR=1 \
    bash "$repo_root/.agents/swarm.sh" logs --follow scout worker
)"

assert_text_includes "$follow_output" $'\033[36m[scout]\033[0m scout sample line'
assert_text_includes "$follow_output" $'\033[32m[worker]\033[0m worker sample line'

swarm_env bash "$repo_root/.agents/swarm.sh" stop >/dev/null 2>&1

rm -f "$capture_root/worker.exec" "$capture_root/worker.prompt" "$capture_root/worker.review" "$capture_root/worker.review_prompt"

swarm_env \
  HIVEMIND_WORKER_PRIORITY_LABELS="bug,priority: high,help wanted" \
  bash "$repo_root/.agents/worker-loop.sh" "$worktree_root/worker" >/dev/null 2>&1 &
priority_worker_pid="$!"

wait_for_file "$capture_root/worker.prompt"
wait_for_process_exit "$priority_worker_pid"
assert_file_contains "$capture_root/worker.prompt" "Configured priority labels: bug, priority: high, help wanted."
assert_file_contains "$capture_root/worker.prompt" "Filter eligibility first, including active branch, open PR, and worker-lane checks."
assert_file_contains "$capture_root/worker.prompt" "Prefer eligible issues matching priority labels in the configured order before falling back to the smallest eligible open issue."

rm -f "$capture_root/worker.exec" "$capture_root/worker.prompt" "$capture_root/worker.review" "$capture_root/worker.review_prompt"

emoji_priority_labels=$'\360\237\220\235 swarm,needs:review?,release@night'
emoji_priority_display=$'\360\237\220\235 swarm, needs:review?, release@night.'

swarm_env \
  HIVEMIND_WORKER_PRIORITY_LABELS="$emoji_priority_labels" \
  bash "$repo_root/.agents/worker-loop.sh" "$worktree_root/worker" >/dev/null 2>&1 &
emoji_worker_pid="$!"

wait_for_file "$capture_root/worker.prompt"
wait_for_process_exit "$emoji_worker_pid"
assert_file_contains "$capture_root/worker.prompt" "Configured priority labels: $emoji_priority_display"

rm -f "$capture_root"/*.exec "$capture_root"/*.prompt "$capture_root"/*.review

swarm_env bash "$repo_root/.agents/swarm.sh" start reviewer-1 developer feature-requester-1 browser-user pr-shepherd >/dev/null

for role in reviewer worker feature-requester scout beekeeper; do
  wait_for_file "$capture_root/$role.exec"
done
wait_for_file "$capture_root/worker.review"

alias_status_output="$(swarm_env bash "$repo_root/.agents/swarm.sh" status reviewer-1 developer feature-requester-1 browser-user pr-shepherd)"
for role in reviewer worker feature-requester scout beekeeper; do
  if [[ "$alias_status_output" != *"$role"* ]]; then
    echo "alias status output missing $role" >&2
    echo "$alias_status_output" >&2
    exit 1
  fi
done

swarm_env bash "$repo_root/.agents/swarm.sh" stop >/dev/null 2>&1

rm -f "$capture_root"/*.exec "$capture_root"/*.prompt "$capture_root"/*.review

swarm_env bash "$repo_root/.agents/swarm.sh" start --reviewers 2 --workers 3 --feature-requesters 2 --scouts 1 --pr-shepherds 1 >/dev/null

for role in reviewer worker feature-requester scout beekeeper; do
  wait_for_file "$capture_root/$role.exec"
done

swarm_env bash "$repo_root/.agents/swarm.sh" stop >/dev/null 2>&1

rm -f "$capture_root/worker.exec" "$capture_root/worker.prompt" "$capture_root/worker.review" "$capture_root/worker.review_prompt"

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HOME="$home_root" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
HIVEMIND_WORKER_A_MAX_RUNS=1 \
HIVEMIND_WORKER_A_SLEEP_SECONDS=0 \
HIVEMIND_WORKER_A_REVIEW_PROMPT="Legacy worker A review prompt" \
bash "$repo_root/.agents/worker-loop-a.sh" "$worktree_root/worker" >/dev/null 2>&1 &
worker_a_pid="$!"

wait_for_file "$capture_root/worker.review_prompt"
wait_for_process_exit "$worker_a_pid"
assert_file_contains "$capture_root/worker.review_prompt" "Legacy worker A review prompt"

rm -f "$capture_root/worker.exec" "$capture_root/worker.prompt" "$capture_root/worker.review" "$capture_root/worker.review_prompt"

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HOME="$home_root" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
HIVEMIND_DEVELOPER_MAX_RUNS=1 \
HIVEMIND_DEVELOPER_SLEEP_SECONDS=0 \
HIVEMIND_DEVELOPER_REVIEW_PROMPT="Legacy developer review prompt" \
bash "$repo_root/.agents/developer-loop.sh" "$worktree_root/worker" >/dev/null 2>&1 &
developer_pid="$!"

wait_for_file "$capture_root/worker.review_prompt"
wait_for_process_exit "$developer_pid"
assert_file_contains "$capture_root/worker.review_prompt" "Legacy developer review prompt"

rm -f "$capture_root/worker.exec" "$capture_root/worker.prompt" "$capture_root/worker.review" "$capture_root/worker.review_prompt"

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HOME="$home_root" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
HIVEMIND_WORKER_B_MAX_RUNS=1 \
HIVEMIND_WORKER_B_SLEEP_SECONDS=0 \
HIVEMIND_WORKER_B_REVIEW_PROMPT="Legacy worker B review prompt" \
bash "$repo_root/.agents/worker-loop-b.sh" "$worktree_root/worker" >/dev/null 2>&1 &
worker_b_pid="$!"

wait_for_file "$capture_root/worker.review_prompt"
assert_file_contains "$capture_root/worker.review_prompt" "Legacy worker B review prompt"
wait_for_process_exit "$worker_b_pid"

rm -f "$capture_root/scout.exec" "$capture_root/scout.prompt"

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HOME="$home_root" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
HIVEMIND_BROWSER_USER_MAX_RUNS=1 \
HIVEMIND_BROWSER_USER_SLEEP_SECONDS=0 \
bash "$repo_root/.agents/browser-user-loop.sh" "$worktree_root/scout" >/dev/null 2>&1 &
browser_user_pid="$!"

wait_for_file "$capture_root/scout.exec"
wait_for_process_exit "$browser_user_pid"

rm -f "$capture_root/beekeeper.exec" "$capture_root/beekeeper.prompt"

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HOME="$home_root" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
HIVEMIND_PR_SHEPHERD_MAX_RUNS=1 \
HIVEMIND_PR_SHEPHERD_SLEEP_SECONDS=0 \
bash "$repo_root/.agents/pr-shepherd.sh" "$worktree_root/beekeeper" >/dev/null 2>&1 &
pr_shepherd_pid="$!"

wait_for_file "$capture_root/beekeeper.exec"
wait_for_process_exit "$pr_shepherd_pid"

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HOME="$home_root" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$case_root/runtime-supervisor" \
HIVEMIND_SWARM_WORKTREE_ROOT="$case_root/worktrees-supervisor" \
HIVEMIND_SWARM_SUPERVISOR_SLEEP_SECONDS=1 \
HIVEMIND_WORKER_MAX_RUNS=1 \
HIVEMIND_WORKER_SLEEP_SECONDS=0 \
bash "$repo_root/.agents/swarm.sh" run worker-1 >/dev/null 2>&1 &
supervisor_pid="$!"

wait_for_line_count "$capture_root/worker.exec" 2
wait_for_line_count "$capture_root/worker.review" 2

kill "$supervisor_pid" >/dev/null 2>&1 || true
wait "$supervisor_pid" >/dev/null 2>&1 || true

if [[ "$(uname -s)" == "Darwin" ]]; then
  plist_output="$(
    PATH="$bin_root:$PATH" \
    CODEX_SANDBOX="" \
    HOME="$home_root" \
    bash "$repo_root/.agents/swarm-launchd.sh" print-plist worker beekeeper
  )"

  assert_text_includes "$plist_output" "<string>run</string>"
  assert_text_includes "$plist_output" "<string>worker</string>"
  assert_text_includes "$plist_output" "<string>beekeeper</string>"
  assert_text_includes "$plist_output" "<key>KeepAlive</key>"
  assert_text_includes "$plist_output" "<key>RunAtLoad</key>"

  plist_file="$case_root/swarm-launchd.plist"
  printf '%s\n' "$plist_output" >"$plist_file"
  plutil -lint "$plist_file" >/dev/null
fi

rm -rf "$case_root"
echo "[swarm-verify] all swarm checks passed"
