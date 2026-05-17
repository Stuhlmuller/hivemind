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
  cp "$source_repo_root/.agents/worker-loop-a.sh" "$repo_root/.agents/worker-loop-a.sh"
  cp "$source_repo_root/.agents/worker-loop-b.sh" "$repo_root/.agents/worker-loop-b.sh"
  cp "$source_repo_root/.agents/pr-shepherd.sh" "$repo_root/.agents/pr-shepherd.sh"
  cp "$source_repo_root/.agents/swarm.sh" "$repo_root/.agents/swarm.sh"
  cp "$source_repo_root/.agents/PROMPT-scout.md" "$repo_root/.agents/PROMPT-scout.md"
  cp "$source_repo_root/.agents/PROMPT-worker-a.md" "$repo_root/.agents/PROMPT-worker-a.md"
  cp "$source_repo_root/.agents/PROMPT-worker-b.md" "$repo_root/.agents/PROMPT-worker-b.md"
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
    "$repo_root/.agents/worker-loop-a.sh" \
    "$repo_root/.agents/worker-loop-b.sh" \
    "$repo_root/.agents/pr-shepherd.sh" \
    "$repo_root/.agents/swarm.sh"
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
    printf '%s\n' "$run_root" >"${HIVEMIND_SWARM_CAPTURE_DIR:?missing}/$HIVEMIND_LOOP_LABEL.exec"
    exit 0
    ;;
  review)
    pwd >"${HIVEMIND_SWARM_CAPTURE_DIR:?missing}/$HIVEMIND_LOOP_LABEL.review"
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

assert_file_equals() {
  local file_path="$1"
  local expected_path="$2"

  if [[ "$(canonicalize_path "$(cat "$file_path")")" == "$(canonicalize_path "$expected_path")" ]]; then
    return
  fi

  echo "unexpected path in $file_path" >&2
  cat "$file_path" >&2
  exit 1
}

case_root="$(mktemp -d "${TMPDIR:-/tmp}/swarm-check.XXXXXX")"
bin_root="$case_root/bin"
capture_root="$case_root/capture"
runtime_root="$case_root/runtime"
worktree_root="$case_root/worktrees"

mkdir -p "$bin_root" "$capture_root"
write_stub_gh "$bin_root"
write_stub_codex "$bin_root"
repo_root="$(setup_case_repo "$case_root")"

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
HIVEMIND_SCOUT_MAX_RUNS=1 \
HIVEMIND_SCOUT_SLEEP_SECONDS=0 \
HIVEMIND_WORKER_A_MAX_RUNS=1 \
HIVEMIND_WORKER_A_SLEEP_SECONDS=0 \
HIVEMIND_WORKER_B_MAX_RUNS=1 \
HIVEMIND_WORKER_B_SLEEP_SECONDS=0 \
HIVEMIND_PR_SHEPHERD_MAX_RUNS=1 \
HIVEMIND_PR_SHEPHERD_SLEEP_SECONDS=0 \
bash "$repo_root/.agents/swarm.sh" start >/dev/null

wait_for_file "$capture_root/scout.exec"
wait_for_file "$capture_root/worker-a.exec"
wait_for_file "$capture_root/worker-b.exec"
wait_for_file "$capture_root/pr-shepherd.exec"
wait_for_file "$capture_root/worker-a.review"
wait_for_file "$capture_root/worker-b.review"

assert_file_equals "$capture_root/scout.exec" "$worktree_root/scout"
assert_file_equals "$capture_root/worker-a.exec" "$worktree_root/worker-a"
assert_file_equals "$capture_root/worker-b.exec" "$worktree_root/worker-b"
assert_file_equals "$capture_root/pr-shepherd.exec" "$worktree_root/pr-shepherd"
assert_file_equals "$capture_root/worker-a.review" "$worktree_root/worker-a"
assert_file_equals "$capture_root/worker-b.review" "$worktree_root/worker-b"

status_output="$(
  PATH="$bin_root:$PATH" \
  CODEX_SANDBOX="" \
  HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
  HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
  HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
  bash "$repo_root/.agents/swarm.sh" status
)"

for role in scout worker-a worker-b pr-shepherd; do
  if [[ "$status_output" != *"$role"* ]]; then
    echo "status output missing $role" >&2
    echo "$status_output" >&2
    exit 1
  fi
done

PATH="$bin_root:$PATH" \
CODEX_SANDBOX="" \
HIVEMIND_SWARM_CAPTURE_DIR="$capture_root" \
HIVEMIND_SWARM_RUNTIME_ROOT="$runtime_root" \
HIVEMIND_SWARM_WORKTREE_ROOT="$worktree_root" \
bash "$repo_root/.agents/swarm.sh" stop >/dev/null

rm -rf "$case_root"
echo "[swarm-verify] all swarm checks passed"
