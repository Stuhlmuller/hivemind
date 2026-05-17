#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_ralph="$repo_root/.agents/ralph.sh"
source_prompt="$repo_root/.agents/PROMPT.md"
source_tools="$repo_root/.agents/TOOLS.md"
source_flake="$repo_root/flake.nix"

tmp_root="$(mktemp -d)"
trap 'rm -rf "$tmp_root"' EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_eq() {
  local actual="$1"
  local expected="$2"
  local label="$3"

  if [[ "$actual" != "$expected" ]]; then
    fail "$label: expected '$expected' but got '$actual'"
  fi
}

assert_file_contains() {
  local file="$1"
  local needle="$2"

  if ! grep -F -- "$needle" "$file" >/dev/null; then
    fail "expected '$needle' in $file"
  fi
}

canonical_path() {
  local path="$1"
  (
    cd "$path"
    pwd -P
  )
}

setup_fixture_repo() {
  local name="$1"
  local fixture_root="$tmp_root/$name"
  local repo="$fixture_root/repo"
  local bin_dir="$fixture_root/bin"

  mkdir -p "$repo/.agents" "$bin_dir"
  cp "$source_ralph" "$repo/.agents/ralph.sh"
  cp "$source_prompt" "$repo/.agents/PROMPT.md"
  cp "$source_tools" "$repo/.agents/TOOLS.md"
  cp "$source_flake" "$repo/flake.nix"

  git init -q -b main "$repo"
  git -C "$repo" config user.name "Test User"
  git -C "$repo" config user.email "test@example.com"
  git -C "$repo" add .agents/ralph.sh .agents/PROMPT.md .agents/TOOLS.md flake.nix
  git -C "$repo" -c commit.gpgsign=false commit -q -m "fixture"
  git -C "$repo" update-ref refs/remotes/origin/main HEAD
  git -C "$repo" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main

  cat >"$bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ge 2 && "$1" == "auth" && "$2" == "status" ]]; then
  exit 0
fi

if [[ "$#" -ge 2 && "$1" == "issue" && "$2" == "list" ]]; then
  exit 0
fi

printf 'unexpected gh args: %s\n' "$*" >&2
exit 1
EOF
  chmod +x "$bin_dir/gh"

  cat >"$bin_dir/codex" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "$1" == "review" ]]; then
  printf '%s\n' "$PWD" >"${RALPH_TEST_REVIEW_LOG:?}"
  exit 0
fi

if [[ "$1" != "exec" ]]; then
  printf 'unexpected codex mode: %s\n' "$1" >&2
  exit 1
fi
shift

run_root=""
sandbox=""

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -C)
      run_root="$2"
      shift 2
      ;;
    -s)
      sandbox="$2"
      shift 2
      ;;
    *)
      break
      ;;
  esac
done

printf '%s\n' "$sandbox" >"${RALPH_TEST_SANDBOX_LOG:?}"

case "${RALPH_TEST_SCENARIO:?}" in
  direct_worktree)
    git -C "$run_root" worktree add -q -b issue-52-demo "${RALPH_TEST_TMPDIR:?}/issue-52-demo" main >/dev/null
    ;;
  local_checkout_then_worktree)
    git -C "$run_root" checkout -q -b issue-52-demo
    git -C "$run_root" checkout -q main
    git -C "$run_root" worktree add -q "${RALPH_TEST_TMPDIR:?}/issue-52-demo" issue-52-demo >/dev/null
    ;;
  stay_same_issue_worktree)
    :
    ;;
  repurpose_issue_worktree)
    git -C "$run_root" checkout -q -b issue-53-demo
    ;;
  *)
    printf 'unknown scenario: %s\n' "${RALPH_TEST_SCENARIO:?}" >&2
    exit 1
    ;;
esac
EOF
  chmod +x "$bin_dir/codex"

  printf '%s\t%s\n' "$repo" "$bin_dir"
}

run_ralph() {
  local script_path="$1"
  local bin_dir="$2"
  local scenario="$3"
  local scenario_tmp="$4"
  local review_log="$5"
  local sandbox_log="$6"
  local stdout_log="$7"
  local stderr_log="$8"

  mkdir -p "$scenario_tmp"

  env -u CODEX_SANDBOX \
    PATH="$bin_dir:$PATH" \
    RALPH_MAX_RUNS=1 \
    RALPH_TEST_SCENARIO="$scenario" \
    RALPH_TEST_TMPDIR="$scenario_tmp" \
    RALPH_TEST_REVIEW_LOG="$review_log" \
    RALPH_TEST_SANDBOX_LOG="$sandbox_log" \
    bash "$script_path" >"$stdout_log" 2>"$stderr_log"
}

test_accepts_direct_issue_worktree_creation() {
  local repo
  local bin_dir
  IFS=$'\t' read -r repo bin_dir <<<"$(setup_fixture_repo direct-worktree)"

  local fixture_root="$tmp_root/direct-worktree"
  local stdout_log="$fixture_root/stdout.log"
  local stderr_log="$fixture_root/stderr.log"
  local review_log="$fixture_root/review.log"
  local sandbox_log="$fixture_root/sandbox.log"
  local scenario_tmp="$fixture_root/runtime"
  local worktree_path="$scenario_tmp/issue-52-demo"

  run_ralph "$repo/.agents/ralph.sh" "$bin_dir" "direct_worktree" "$scenario_tmp" "$review_log" "$sandbox_log" "$stdout_log" "$stderr_log"

  assert_eq "$(cat "$sandbox_log")" "danger-full-access" "sandbox flag"
  assert_eq "$(cat "$review_log")" "$(canonical_path "$worktree_path")" "auto-review worktree"
  assert_eq "$(git -C "$repo" branch --show-current)" "main" "primary checkout branch"
  assert_file_contains "$stdout_log" "[ralph] starting auto-review for run 1 in $(canonical_path "$worktree_path")"
}

test_rejects_local_issue_checkout_before_worktree_creation() {
  local repo
  local bin_dir
  IFS=$'\t' read -r repo bin_dir <<<"$(setup_fixture_repo local-checkout)"

  local fixture_root="$tmp_root/local-checkout"
  local stdout_log="$fixture_root/stdout.log"
  local stderr_log="$fixture_root/stderr.log"
  local review_log="$fixture_root/review.log"
  local sandbox_log="$fixture_root/sandbox.log"
  local scenario_tmp="$fixture_root/runtime"

  if run_ralph "$repo/.agents/ralph.sh" "$bin_dir" "local_checkout_then_worktree" "$scenario_tmp" "$review_log" "$sandbox_log" "$stdout_log" "$stderr_log"; then
    fail "expected Ralph to reject an in-place issue checkout before worktree creation"
  fi

  if [[ -e "$review_log" ]]; then
    fail "auto-review should not run after an in-place checkout failure"
  fi

  assert_file_contains "$stderr_log" "checked out an issue branch in the primary checkout"
}

test_accepts_existing_issue_worktree_without_branch_switching() {
  local repo
  local bin_dir
  IFS=$'\t' read -r repo bin_dir <<<"$(setup_fixture_repo stay-in-worktree)"

  local fixture_root="$tmp_root/stay-in-worktree"
  local stdout_log="$fixture_root/stdout.log"
  local stderr_log="$fixture_root/stderr.log"
  local review_log="$fixture_root/review.log"
  local sandbox_log="$fixture_root/sandbox.log"
  local scenario_tmp="$fixture_root/runtime"
  local issue_worktree="$fixture_root/issue-52-demo"

  git -C "$repo" worktree add -q -b issue-52-demo "$issue_worktree" main
  run_ralph "$issue_worktree/.agents/ralph.sh" "$bin_dir" "stay_same_issue_worktree" "$scenario_tmp" "$review_log" "$sandbox_log" "$stdout_log" "$stderr_log"

  assert_eq "$(cat "$review_log")" "$(canonical_path "$issue_worktree")" "review stayed in issue worktree"
  assert_file_contains "$stdout_log" "[ralph] starting Codex run 1 in $(canonical_path "$issue_worktree")"
}

test_rejects_repurposing_issue_worktree_with_new_branch_checkout() {
  local repo
  local bin_dir
  IFS=$'\t' read -r repo bin_dir <<<"$(setup_fixture_repo repurpose-worktree)"

  local fixture_root="$tmp_root/repurpose-worktree"
  local stdout_log="$fixture_root/stdout.log"
  local stderr_log="$fixture_root/stderr.log"
  local review_log="$fixture_root/review.log"
  local sandbox_log="$fixture_root/sandbox.log"
  local scenario_tmp="$fixture_root/runtime"
  local issue_worktree="$fixture_root/issue-52-demo"

  git -C "$repo" worktree add -q -b issue-52-demo "$issue_worktree" main

  if run_ralph "$issue_worktree/.agents/ralph.sh" "$bin_dir" "repurpose_issue_worktree" "$scenario_tmp" "$review_log" "$sandbox_log" "$stdout_log" "$stderr_log"; then
    fail "expected Ralph to reject reusing an issue worktree for a different issue branch"
  fi

  if [[ -e "$review_log" ]]; then
    fail "auto-review should not run after repurposing an issue worktree"
  fi

  assert_file_contains "$stderr_log" "requires a fresh git worktree for each issue branch"
}

test_accepts_direct_issue_worktree_creation
test_rejects_local_issue_checkout_before_worktree_creation
test_accepts_existing_issue_worktree_without_branch_switching
test_rejects_repurposing_issue_worktree_with_new_branch_checkout

printf 'Ralph worktree loop tests passed.\n'
