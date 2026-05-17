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
  cp "$source_repo_root/.agents/role-loop.sh" "$repo_root/.agents/role-loop.sh"
  cp "$source_repo_root/.agents/scout-loop.sh" "$repo_root/.agents/scout-loop.sh"
  cp "$source_repo_root/.agents/browser-user-loop.sh" "$repo_root/.agents/browser-user-loop.sh"
  cp "$source_repo_root/.agents/reviewer-loop.sh" "$repo_root/.agents/reviewer-loop.sh"
  cp "$source_repo_root/.agents/worker-loop.sh" "$repo_root/.agents/worker-loop.sh"
  cp "$source_repo_root/.agents/developer-loop.sh" "$repo_root/.agents/developer-loop.sh"
  cp "$source_repo_root/.agents/worker-loop-a.sh" "$repo_root/.agents/worker-loop-a.sh"
  cp "$source_repo_root/.agents/worker-loop-b.sh" "$repo_root/.agents/worker-loop-b.sh"
  cp "$source_repo_root/.agents/feature-requester-loop.sh" "$repo_root/.agents/feature-requester-loop.sh"
  cp "$source_repo_root/.agents/pr-shepherd.sh" "$repo_root/.agents/pr-shepherd.sh"
  cp "$source_repo_root/.agents/swarm.sh" "$repo_root/.agents/swarm.sh"
  cp "$source_repo_root/.agents/swarm-launchd.sh" "$repo_root/.agents/swarm-launchd.sh"
  cp "$source_repo_root/.agents/PROMPT-subagents.md" "$repo_root/.agents/PROMPT-subagents.md"
  cp "$source_repo_root/.agents/PROMPT-scout.md" "$repo_root/.agents/PROMPT-scout.md"
  cp "$source_repo_root/.agents/PROMPT-reviewer.md" "$repo_root/.agents/PROMPT-reviewer.md"
  cp "$source_repo_root/.agents/PROMPT-worker.md" "$repo_root/.agents/PROMPT-worker.md"
  cp "$source_repo_root/.agents/PROMPT-feature-requester.md" "$repo_root/.agents/PROMPT-feature-requester.md"
  cp "$source_repo_root/.agents/PROMPT-pr-shepherd.md" "$repo_root/.agents/PROMPT-pr-shepherd.md"
  cp "$source_repo_root/.agents/TOOLS.md" "$repo_root/.agents/TOOLS.md"
  cp "$source_repo_root/.agents/SWARM.md" "$repo_root/.agents/SWARM.md"
  cp "$source_repo_root/.agents/skills/hivemind-github-swarm-loop/SKILL.md" "$repo_root/.agents/skills/hivemind-github-swarm-loop/SKILL.md"
  cp "$source_repo_root/.agents/skills/hivemind-github-swarm-loop/agents/openai.yaml" "$repo_root/.agents/skills/hivemind-github-swarm-loop/agents/openai.yaml"
  cp "$source_repo_root/flake.nix" "$repo_root/flake.nix"

  chmod +x \
    "$repo_root/.agents/loop-common.sh" \
    "$repo_root/.agents/role-loop.sh" \
    "$repo_root/.agents/scout-loop.sh" \
    "$repo_root/.agents/browser-user-loop.sh" \
    "$repo_root/.agents/reviewer-loop.sh" \
    "$repo_root/.agents/worker-loop.sh" \
    "$repo_root/.agents/developer-loop.sh" \
    "$repo_root/.agents/worker-loop-a.sh" \
    "$repo_root/.agents/worker-loop-b.sh" \
    "$repo_root/.agents/feature-requester-loop.sh" \
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
HIVEMIND_PR_SHEPHERD_MAX_RUNS=1 \
HIVEMIND_PR_SHEPHERD_SLEEP_SECONDS=0 \
bash "$repo_root/.agents/swarm.sh" start --reviewers 2 --workers 3 --feature-requesters 2 --scouts 1 --pr-shepherds 1 >/dev/null

for role in reviewer-1 reviewer-2 worker-1 worker-2 worker-3 feature-requester-1 feature-requester-2 scout-1 pr-shepherd-1; do
  wait_for_file "$capture_root/$role.exec"
  wait_for_file "$capture_root/$role.prompt"
done

for role in worker-1 worker-2 worker-3; do
  wait_for_file "$capture_root/$role.review"
done

assert_file_equals "$capture_root/reviewer-1.exec" "$worktree_root/reviewer-1"
assert_file_equals "$capture_root/worker-2.exec" "$worktree_root/worker-2"
assert_file_equals "$capture_root/feature-requester-2.exec" "$worktree_root/feature-requester-2"
assert_file_equals "$capture_root/scout-1.exec" "$worktree_root/scout-1"
assert_file_equals "$capture_root/pr-shepherd-1.exec" "$worktree_root/pr-shepherd-1"
assert_file_equals "$capture_root/worker-1.review" "$worktree_root/worker-1"

for role in reviewer-1 worker-2 feature-requester-2 scout-1 pr-shepherd-1; do
  assert_prompt_includes_subagent_policy "$capture_root/$role.prompt"
done

assert_file_contains "$capture_root/scout-1.prompt" "This is the only loop allowed to use the Codex browser tool."
assert_file_contains "$capture_root/scout-1.prompt" "You are \`scout-1\`, scout lane 1 of 1."
assert_file_contains "$capture_root/reviewer-1.prompt" "You are \`reviewer-1\`, reviewer lane 1 of 2."
assert_file_contains "$capture_root/worker-2.prompt" "You are \`worker-2\`, worker lane 2 of 3."
assert_file_contains "$capture_root/worker-2.prompt" "((issue_number - 1) % 3) + 1 == 2"
assert_file_contains "$capture_root/feature-requester-2.prompt" "You are \`feature-requester-2\`, feature-requester lane 2 of 2."
assert_file_contains "$capture_root/pr-shepherd-1.prompt" "You are \`pr-shepherd-1\`, PR shepherd lane 1 of 1."
assert_file_contains "$capture_root/reviewer-1.prompt" "Do not use the Codex browser tool in this loop."
assert_file_contains "$capture_root/worker-2.prompt" "Do not use the Codex browser tool. Leave live browser validation to the main-branch scout lane."
assert_file_contains "$capture_root/feature-requester-2.prompt" "Do not use the Codex browser tool in this loop."
assert_file_contains "$capture_root/pr-shepherd-1.prompt" "Do not use the Codex browser tool. Leave live browser validation to the main-branch scout lane."

status_output="$(
  PATH="$bin_root:$PATH" \
  CODEX_SANDBOX="" \
  HOME="$home_root" \
  HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
  HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
  HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
  bash "$repo_root/.agents/swarm.sh" status
)"

for role in reviewer-1 worker-3 feature-requester-2 scout-1 pr-shepherd-1; do
  if [[ "$status_output" != *"$role"* ]]; then
    echo "status output missing $role" >&2
    echo "$status_output" >&2
    exit 1
  fi
done

printf 'scout-1 sample line\n' >>"$runtime_root/logs/scout-1.log"
printf 'worker-1 sample line\n' >>"$runtime_root/logs/worker-1.log"

follow_output="$(
  PATH="$bin_root:$PATH" \
  CODEX_SANDBOX="" \
  HOME="$home_root" \
  HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
  HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
  HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
  HIVEMIND_SWARM_TAIL_LINES=1 \
  HIVEMIND_SWARM_FOLLOW_MAX_LINES=2 \
  HIVEMIND_SWARM_FORCE_COLOR=1 \
  bash "$repo_root/.agents/swarm.sh" logs --follow scout-1 worker-1
)"

assert_text_includes "$follow_output" $'\033[36m[scout-1]\033[0m scout-1 sample line'
assert_text_includes "$follow_output" $'\033[32m[worker-1]\033[0m worker-1 sample line'

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HOME="$home_root" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
bash "$repo_root/.agents/swarm.sh" stop >/dev/null

rm -f "$capture_root/worker-1.exec" "$capture_root/worker-1.prompt" "$capture_root/worker-1.review"

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HOME="$home_root" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$case_root/runtime-supervisor" \
HIVEMIND_SWARM_WORKTREE_ROOT="$case_root/worktrees-supervisor" \
HIVEMIND_SWARM_SUPERVISOR_SLEEP_SECONDS=1 \
HIVEMIND_WORKER_MAX_RUNS=1 \
HIVEMIND_WORKER_SLEEP_SECONDS=0 \
bash "$repo_root/.agents/swarm.sh" run --workers 2 worker-1 >/dev/null 2>&1 &
supervisor_pid="$!"

wait_for_line_count "$capture_root/worker-1.exec" 2
wait_for_line_count "$capture_root/worker-1.review" 2

kill "$supervisor_pid" >/dev/null 2>&1 || true
wait "$supervisor_pid" >/dev/null 2>&1 || true

if [[ "$(uname -s)" == "Darwin" ]]; then
  plist_output="$(
    PATH="$bin_root:$PATH" \
    CODEX_SANDBOX="" \
    HOME="$home_root" \
    bash "$repo_root/.agents/swarm-launchd.sh" print-plist --workers 3 --scouts 1 worker-1 pr-shepherd-1
  )"

  assert_text_includes "$plist_output" "<string>run</string>"
  assert_text_includes "$plist_output" "<string>--workers</string>"
  assert_text_includes "$plist_output" "<string>3</string>"
  assert_text_includes "$plist_output" "<string>worker-1</string>"
  assert_text_includes "$plist_output" "<string>pr-shepherd-1</string>"
  assert_text_includes "$plist_output" "<key>KeepAlive</key>"
  assert_text_includes "$plist_output" "<key>RunAtLoad</key>"

  plist_file="$case_root/swarm-launchd.plist"
  printf '%s\n' "$plist_output" >"$plist_file"
  plutil -lint "$plist_file" >/dev/null
fi

rm -rf "$case_root"
echo "[swarm-verify] all swarm checks passed"
