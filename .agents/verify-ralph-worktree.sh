#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_repo_root="$(cd "$script_dir/.." && pwd)"
source_ralph="$script_dir/ralph.sh"
source_prompt="$script_dir/PROMPT.md"
source_tools="$script_dir/TOOLS.md"
source_flake="$source_repo_root/flake.nix"

canonicalize_path() {
  local target_path="$1"
  (
    cd "$target_path"
    pwd -P
  )
}

setup_case_repo() {
  local case_root="$1"
  local layout="${2:-standard}"
  local repo_root="$case_root/repo"
  local remote_root="$case_root/remote.git"

  git init --bare "$remote_root" >/dev/null
  case "$layout" in
    standard)
      git clone "$remote_root" "$repo_root" >/dev/null 2>&1
      ;;
    separate-git-dir)
      git clone --separate-git-dir "$case_root/repo.gitdir" "$remote_root" "$repo_root" >/dev/null 2>&1
      ;;
    *)
      echo "unsupported layout: $layout" >&2
      exit 1
      ;;
  esac

  mkdir -p "$repo_root/.agents"
  cp "$source_ralph" "$repo_root/.agents/ralph.sh"
  cp "$source_prompt" "$repo_root/.agents/PROMPT.md"
  cp "$source_tools" "$repo_root/.agents/TOOLS.md"
  cp "$source_flake" "$repo_root/flake.nix"

  (
    cd "$repo_root"
    git config user.name "Ralph Test"
    git config user.email "ralph-test@example.com"
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
    case "${RALPH_STUB_SCENARIO:?missing RALPH_STUB_SCENARIO}" in
      no-op)
        exit 0
        ;;
      no-issue-branch)
        exit 0
        ;;
      in-place-issue-branch)
        git -C "$run_root" switch -c issue-99-test-branch >/dev/null 2>&1
        exit 0
        ;;
      dedicated-issue-worktree)
        worktree_root="${RALPH_STUB_WORKTREE_PATH:?missing RALPH_STUB_WORKTREE_PATH}"
        git -C "$run_root" worktree add -b issue-99-test-branch "$worktree_root" origin/main >/dev/null 2>&1
        exit 0
        ;;
    esac
    ;;
  review)
    pwd >"${RALPH_STUB_REVIEW_CWD_FILE:?missing RALPH_STUB_REVIEW_CWD_FILE}"
    exit 0
    ;;
esac

echo "unexpected codex invocation: $command_name $*" >&2
exit 1
EOF

  chmod +x "$bin_root/codex"
}

run_case() {
  local scenario="$1"
  local expected_status="$2"
  local expected_text="$3"
  local verify_review_path="$4"
  local case_root
  local bin_root
  local repo_root
  local run_root
  local review_cwd_file
  local worktree_path
  local output
  local status
  local setup_layout="standard"

  case_root="$(mktemp -d "${TMPDIR:-/tmp}/ralph-worktree-check.XXXXXX")"
  bin_root="$case_root/bin"
  mkdir -p "$bin_root"

  write_stub_gh "$bin_root"
  write_stub_codex "$bin_root"
  case "$scenario" in
    existing-primary-issue-branch-separate-git-dir)
      setup_layout="separate-git-dir"
      ;;
  esac

  repo_root="$(setup_case_repo "$case_root" "$setup_layout")"
  run_root="$repo_root"
  review_cwd_file="$case_root/review-cwd.txt"
  worktree_path="$case_root/issue-99-test-branch"

  case "$scenario" in
    existing-primary-issue-branch)
      git -C "$repo_root" switch -c issue-99-test-branch >/dev/null 2>&1
      scenario="no-op"
      ;;
    existing-primary-issue-branch-separate-git-dir)
      git -C "$repo_root" switch -c issue-99-test-branch >/dev/null 2>&1
      scenario="no-op"
      ;;
    existing-issue-worktree)
      git -C "$repo_root" worktree add -b issue-99-test-branch "$worktree_path" origin/main >/dev/null 2>&1
      run_root="$worktree_path"
      scenario="no-op"
      ;;
  esac

  set +e
  output="$(
    PATH="$bin_root:$PATH" \
    RALPH_MAX_RUNS=1 \
    RALPH_STUB_SCENARIO="$scenario" \
    RALPH_STUB_REVIEW_CWD_FILE="$review_cwd_file" \
    RALPH_STUB_WORKTREE_PATH="$worktree_path" \
    CODEX_SANDBOX="" \
    bash "$run_root/.agents/ralph.sh" 2>&1
  )"
  status=$?
  set -e

  if [[ "$expected_status" == "success" && "$status" -ne 0 ]]; then
    echo "case '$scenario' failed unexpectedly" >&2
    echo "$output" >&2
    exit 1
  fi

  if [[ "$expected_status" == "failure" && "$status" -eq 0 ]]; then
    echo "case '$scenario' succeeded unexpectedly" >&2
    echo "$output" >&2
    exit 1
  fi

  if [[ -n "$expected_text" && "$output" != *"$expected_text"* ]]; then
    echo "case '$scenario' did not include expected output: $expected_text" >&2
    echo "$output" >&2
    exit 1
  fi

  if [[ "$verify_review_path" == "yes" ]]; then
    if [[ ! -f "$review_cwd_file" ]]; then
      echo "case '$scenario' did not run codex review" >&2
      exit 1
    fi
    if [[ "$(canonicalize_path "$(cat "$review_cwd_file")")" != "$(canonicalize_path "$worktree_path")" ]]; then
      echo "case '$scenario' reviewed the wrong worktree" >&2
      cat "$review_cwd_file" >&2
      exit 1
    fi
  fi

  rm -rf "$case_root"
}

run_case "no-issue-branch" "failure" "did not create a dedicated issue worktree or continue inside an existing issue worktree" "no"
run_case "in-place-issue-branch" "failure" "issue work must run from a dedicated git worktree" "no"
run_case "existing-primary-issue-branch" "failure" "issue work must run from a dedicated git worktree" "no"
run_case "existing-primary-issue-branch-separate-git-dir" "failure" "issue work must run from a dedicated git worktree" "no"
run_case "dedicated-issue-worktree" "success" "" "yes"
run_case "existing-issue-worktree" "success" "" "yes"

echo "[ralph-verify] all worktree checks passed"
