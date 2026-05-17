#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
prompt_file="$script_dir/PROMPT.md"
tools_file="$script_dir/TOOLS.md"
flake_file="$repo_root/flake.nix"

max_runs="${RALPH_MAX_RUNS:-0}"
sleep_seconds="${RALPH_SLEEP_SECONDS:-0}"
review_prompt="${RALPH_REVIEW_PROMPT:-Review the current uncommitted changes. Prioritize correctness bugs, regressions, and missing tests.}"
iteration=1
completed_runs=0
recovery_prompt=""

trap 'printf "\n[ralph] stopping\n"; exit 0' INT TERM

ensure_not_nested_codex() {
  if [[ -z "${CODEX_SANDBOX:-}" ]]; then
    return
  fi

  echo "[ralph] nested Codex runs are not supported inside a Codex sandbox (${CODEX_SANDBOX})." >&2
  echo "[ralph] run .agents/ralph.sh from a normal terminal session instead." >&2
  exit 1
}

ensure_tools_file() {
  if [[ -f "$tools_file" ]]; then
    return
  fi

  cat >"$tools_file" <<'EOF'
# Agent Tool Manifest

This file must exist on every new agent spawn. Update it before or immediately after using a new CLI so the repository bootstrap stays current.
EOF
}

ensure_flake_file() {
  if [[ -f "$flake_file" ]]; then
    return
  fi

  cat >"$flake_file" <<'EOF'
{
  description = "Hivemind agent development shell";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { nixpkgs, ... }:
    let
      systems = [
        "aarch64-darwin"
        "x86_64-darwin"
        "aarch64-linux"
        "x86_64-linux"
      ];
      forAllSystems = f:
        nixpkgs.lib.genAttrs systems (system: f system);
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              bash
              coreutils
              findutils
              gh
              git
              gnused
              ripgrep
            ];
          };
        });
    };
}
EOF
}

ensure_github_ready() {
  if ! command -v gh >/dev/null 2>&1; then
    echo "[ralph] gh is required in PATH" >&2
    exit 1
  fi

  if ! gh auth status; then
    echo "[ralph] gh authentication is required for Ralph; run 'gh auth login -h github.com' and retry" >&2
    exit 1
  fi

  if ! gh issue list --state all --limit 1 >/dev/null 2>&1; then
    echo "[ralph] gh must be able to read repository issues before Ralph can run" >&2
    exit 1
  fi
}

compose_prompt_text() {
  local run_root="$1"
  local prompt_text

  prompt_text="$(<"$run_root/.agents/PROMPT.md")"
  if [[ -z "$recovery_prompt" ]]; then
    printf '%s' "$prompt_text"
    return
  fi

  cat <<EOF
$prompt_text

## Ralph Recovery Instruction

The previous Ralph attempt hit a recoverable failure.

$recovery_prompt

Fix the blocking issue first, then restart the normal Ralph workflow from the top in this same run:
- verify the current repo and tool state
- inspect the next repository issue to work on
- use exactly one dedicated issue worktree and issue branch
- continue through PR creation and follow-up
Do not stop after diagnosing the problem. Restore the issue-driven flow and keep moving.
EOF
}

set_recovery_prompt() {
  local stage="$1"
  local exit_code="$2"

  case "$stage" in
    codex_exec)
      recovery_prompt="$(cat <<EOF
Failure stage: codex exec
Exit status: $exit_code

Inspect the repo state, identify what blocked the previous Codex run, fix that blocker, and then resume the normal GitHub-driven issue workflow from the correct dedicated issue worktree.
EOF
)"
      ;;
    auto_review)
      recovery_prompt="$(cat <<EOF
Failure stage: codex review
Exit status: $exit_code

Review the current working tree, address the problems that caused the auto-review failure, add any missing verification, and then resume the normal GitHub-driven issue workflow from the correct dedicated issue worktree.
EOF
)"
      ;;
    *)
      echo "[ralph] unknown recovery stage '$stage'" >&2
      exit 1
      ;;
  esac
}

advance_iteration() {
  iteration=$((iteration + 1))

  if [[ "$sleep_seconds" != "0" ]]; then
    sleep "$sleep_seconds"
  fi
}

default_branch_name() {
  local repo_root="$1"
  local ref

  ref="$(git -C "$repo_root" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [[ -z "$ref" ]]; then
    echo "[ralph] unable to determine the default branch from origin/HEAD" >&2
    exit 1
  fi

  printf '%s\n' "${ref#refs/remotes/origin/}"
}

current_branch_name() {
  local repo_root="$1"
  local branch

  branch="$(git -C "$repo_root" branch --show-current)"
  if [[ -z "$branch" ]]; then
    echo "[ralph] Ralph requires a named git branch; detached HEAD is not supported" >&2
    exit 1
  fi

  printf '%s\n' "$branch"
}

is_issue_branch_name() {
  local branch="$1"

  [[ "$branch" =~ ^issue-[0-9]+-[a-z0-9][a-z0-9-]*$ ]]
}

git_absolute_path() {
  local repo_root="$1"
  local rev_parse_flag="$2"
  local git_path

  git_path="$(git -C "$repo_root" rev-parse --path-format=absolute "$rev_parse_flag")"
  canonicalize_path "$git_path"
}

is_primary_checkout_root() {
  local repo_root="$1"
  local git_dir
  local git_common_dir

  git_dir="$(git_absolute_path "$repo_root" --git-dir)"
  git_common_dir="$(git_absolute_path "$repo_root" --git-common-dir)"

  [[ "$git_dir" == "$git_common_dir" ]]
}

canonicalize_path() {
  local target_path="$1"
  (
    cd "$target_path"
    pwd -P
  )
}

head_reflog_count() {
  local repo_root="$1"
  git -C "$repo_root" reflog --format='%gs' | wc -l | tr -d '[:space:]'
}

reflog_includes_checkout() {
  local repo_root="$1"
  local reflog_delta="$2"
  local entry

  if [[ "$reflog_delta" -le 0 ]]; then
    return 1
  fi

  while IFS= read -r entry; do
    if [[ "$entry" == checkout:* ]]; then
      return 0
    fi
  done <<<"$(git -C "$repo_root" reflog --format='%gs' -n "$reflog_delta")"

  return 1
}

reflog_includes_issue_branch_checkout() {
  local repo_root="$1"
  local reflog_delta="$2"
  local entry

  if [[ "$reflog_delta" -le 0 ]]; then
    return 1
  fi

  while IFS= read -r entry; do
    if [[ "$entry" =~ ^checkout:\ moving\ from\ .+\ to\ issue-[0-9]+-[a-z0-9][a-z0-9-]*$ ]]; then
      return 0
    fi
  done <<<"$(git -C "$repo_root" reflog --format='%gs' -n "$reflog_delta")"

  return 1
}

issue_worktree_entries() {
  local repo_root="$1"
  local line
  local path=""
  local branch=""

  while IFS= read -r line; do
    case "$line" in
      worktree\ *)
        if [[ -n "$path" && -n "$branch" ]] && is_issue_branch_name "$branch"; then
          printf '%s\t%s\n' "$branch" "$path"
        fi
        path="${line#worktree }"
        branch=""
        ;;
      branch\ refs/heads/*)
        branch="${line#branch refs/heads/}"
        ;;
      "")
        if [[ -n "$path" && -n "$branch" ]] && is_issue_branch_name "$branch"; then
          printf '%s\t%s\n' "$branch" "$path"
        fi
        path=""
        branch=""
        ;;
    esac
  done <<<"$(git -C "$repo_root" worktree list --porcelain)"

  if [[ -n "$path" && -n "$branch" ]] && is_issue_branch_name "$branch"; then
    printf '%s\t%s\n' "$branch" "$path"
  fi
}

snapshot_includes_worktree_entry() {
  local snapshot="$1"
  local target="$2"
  local entry

  while IFS= read -r entry; do
    if [[ "$entry" == "$target" ]]; then
      return 0
    fi
  done <<<"$snapshot"

  return 1
}

first_new_issue_worktree_path() {
  local repo_root="$1"
  local start_snapshot="$2"
  local normalized_repo_root
  local branch
  local path

  normalized_repo_root="$(canonicalize_path "$repo_root")"

  while IFS=$'\t' read -r branch path; do
    if [[ -z "$branch" || -z "$path" ]]; then
      continue
    fi
    if [[ "$(canonicalize_path "$path")" == "$normalized_repo_root" ]]; then
      continue
    fi
    if ! snapshot_includes_worktree_entry "$start_snapshot" "$branch"$'\t'"$path"; then
      printf '%s\n' "$path"
      return 0
    fi
  done <<<"$(issue_worktree_entries "$repo_root")"

  return 1
}

ensure_branch_context_before_run() {
  local repo_root="$1"
  current_branch_name "$repo_root" >/dev/null
}

ensure_issue_work_runs_in_linked_worktree() {
  local repo_root="$1"
  local branch

  branch="$(current_branch_name "$repo_root")"
  if ! is_issue_branch_name "$branch"; then
    return
  fi

  if is_primary_checkout_root "$repo_root"; then
    echo "[ralph] branch '$branch' is checked out in the primary checkout at '$repo_root'; Ralph issue work must run from a dedicated git worktree" >&2
    exit 1
  fi
}

ensure_issue_branch_activity_after_run() {
  local repo_root="$1"
  local start_branch="$2"
  local start_reflog_count="$3"
  local start_issue_worktrees="$4"
  local default_branch end_branch end_reflog_count reflog_delta
  local new_issue_worktree_path=""
  local saw_checkout="false"
  local saw_issue_checkout="false"

  default_branch="$(default_branch_name "$repo_root")"
  end_branch="$(current_branch_name "$repo_root")"
  end_reflog_count="$(head_reflog_count "$repo_root")"
  reflog_delta=$((end_reflog_count - start_reflog_count))

  if [[ "$end_branch" != "$default_branch" ]] && ! is_issue_branch_name "$end_branch"; then
    echo "[ralph] Codex run ended on non-issue branch '$end_branch'; expected '$default_branch' or issue-<number>-<slug>" >&2
    exit 1
  fi

  if reflog_includes_checkout "$repo_root" "$reflog_delta"; then
    saw_checkout="true"
  fi

  if reflog_includes_issue_branch_checkout "$repo_root" "$reflog_delta"; then
    saw_issue_checkout="true"
  fi

  if new_issue_worktree_path="$(first_new_issue_worktree_path "$repo_root" "$start_issue_worktrees" 2>/dev/null)"; then
    :
  else
    new_issue_worktree_path=""
  fi

  if is_primary_checkout_root "$repo_root"; then
    if [[ "$saw_issue_checkout" == "true" ]]; then
      echo "[ralph] Codex run checked out an issue branch in the primary checkout at '$repo_root'; Ralph must create dedicated git worktrees without local branch checkout" >&2
      exit 1
    fi

    if [[ -n "$new_issue_worktree_path" ]]; then
      printf '%s\n' "$new_issue_worktree_path"
      return
    fi

    if is_issue_branch_name "$end_branch"; then
      echo "[ralph] Codex run ended on issue branch '$end_branch' in the primary checkout at '$repo_root'; Ralph issue work must run from a dedicated git worktree" >&2
      exit 1
    fi

    echo "[ralph] Codex run did not create a dedicated issue worktree. Expected 'git worktree add -b issue-<number>-<slug> ...' from '$default_branch' without checking out the issue branch locally" >&2
    exit 1
  fi

  if [[ "$start_branch" != "$end_branch" ]]; then
    echo "[ralph] Codex run switched from '$start_branch' to '$end_branch' inside worktree '$repo_root'; Ralph requires a fresh git worktree for each issue branch instead of in-place checkout" >&2
    exit 1
  fi

  if [[ "$saw_checkout" == "true" ]]; then
    echo "[ralph] Codex run changed git checkout state inside worktree '$repo_root'; Ralph must stay on the active issue branch or create a fresh worktree for the next issue" >&2
    exit 1
  fi

  if [[ -n "$new_issue_worktree_path" ]]; then
    printf '%s\n' "$new_issue_worktree_path"
    return
  fi

  if ! is_issue_branch_name "$end_branch"; then
    echo "[ralph] Codex run returned to non-issue branch '$end_branch' from worktree '$repo_root'; Ralph issue work must stay on the current issue branch or move into a fresh issue worktree" >&2
    exit 1
  fi

  if [[ "$start_branch" == "$default_branch" && "$end_branch" == "$start_branch" ]]; then
    echo "[ralph] Codex run stayed on '$default_branch'; Ralph requires an issue worktree before continuing" >&2
    exit 1
  fi

  printf '%s\n' "$repo_root"
}

recover_run_root_after_failed_codex_run() {
  local repo_root="$1"
  local start_branch="$2"
  local start_reflog_count="$3"
  local start_issue_worktrees="$4"
  local end_branch end_reflog_count reflog_delta
  local new_issue_worktree_path=""
  local saw_checkout="false"
  local saw_issue_checkout="false"

  end_branch="$(current_branch_name "$repo_root")"
  end_reflog_count="$(head_reflog_count "$repo_root")"
  reflog_delta=$((end_reflog_count - start_reflog_count))

  if reflog_includes_checkout "$repo_root" "$reflog_delta"; then
    saw_checkout="true"
  fi

  if reflog_includes_issue_branch_checkout "$repo_root" "$reflog_delta"; then
    saw_issue_checkout="true"
  fi

  if new_issue_worktree_path="$(first_new_issue_worktree_path "$repo_root" "$start_issue_worktrees" 2>/dev/null)"; then
    :
  else
    new_issue_worktree_path=""
  fi

  if is_primary_checkout_root "$repo_root"; then
    if [[ "$saw_issue_checkout" == "true" ]]; then
      echo "[ralph] Codex run checked out an issue branch in the primary checkout at '$repo_root'; Ralph must create dedicated git worktrees without local branch checkout" >&2
      exit 1
    fi

    if [[ -n "$new_issue_worktree_path" ]]; then
      printf '%s\n' "$new_issue_worktree_path"
      return
    fi

    if is_issue_branch_name "$end_branch"; then
      echo "[ralph] Codex run ended on issue branch '$end_branch' in the primary checkout at '$repo_root'; Ralph issue work must run from a dedicated git worktree" >&2
      exit 1
    fi

    printf '%s\n' "$repo_root"
    return
  fi

  if [[ "$start_branch" != "$end_branch" ]]; then
    echo "[ralph] Codex run switched from '$start_branch' to '$end_branch' inside worktree '$repo_root'; Ralph requires a fresh git worktree for each issue branch instead of in-place checkout" >&2
    exit 1
  fi

  if [[ "$saw_checkout" == "true" ]]; then
    echo "[ralph] Codex run changed git checkout state inside worktree '$repo_root'; Ralph must stay on the active issue branch or create a fresh worktree for the next issue" >&2
    exit 1
  fi

  if [[ -n "$new_issue_worktree_path" ]]; then
    printf '%s\n' "$new_issue_worktree_path"
    return
  fi

  if ! is_issue_branch_name "$end_branch"; then
    echo "[ralph] Codex run returned to non-issue branch '$end_branch' from worktree '$repo_root'; Ralph issue work must stay on the current issue branch or move into a fresh issue worktree" >&2
    exit 1
  fi

  printf '%s\n' "$repo_root"
}

run_codex_exec() {
  local run_root="$1"
  shift
  local prompt_text
  local -a cmd

  prompt_text="$(compose_prompt_text "$run_root")"
  # Ralph must be able to read issues, open PRs, and merge them via `gh`,
  # which requires network access inside the spawned Codex run.
  cmd=(codex exec -C "$run_root" -s danger-full-access)

  if [[ "$#" -gt 0 ]]; then
    cmd+=("$@")
  fi

  cmd+=("$prompt_text")
  "${cmd[@]}"
}

run_auto_review() {
  local run_root="$1"
  (
    cd "$run_root"
    # Codex CLI 0.130.0 rejects `--uncommitted` when a custom prompt is present.
    codex review "$review_prompt"
  )
}

if ! command -v codex >/dev/null 2>&1; then
  echo "[ralph] codex is required in PATH" >&2
  exit 1
fi

if [[ ! -f "$prompt_file" ]]; then
  echo "[ralph] missing prompt file: $prompt_file" >&2
  exit 1
fi

ensure_not_nested_codex
ensure_tools_file
ensure_flake_file
ensure_github_ready

active_run_root="$repo_root"

while :; do
  local_start_branch="$(current_branch_name "$active_run_root")"
  local_start_reflog_count="$(head_reflog_count "$active_run_root")"
  local_start_issue_worktrees="$(issue_worktree_entries "$active_run_root")"

  ensure_github_ready
  ensure_branch_context_before_run "$active_run_root"
  ensure_issue_work_runs_in_linked_worktree "$active_run_root"
  echo "[ralph] starting Codex run $iteration in $active_run_root"
  if run_codex_exec "$active_run_root" "$@"; then
    active_run_root="$(ensure_issue_branch_activity_after_run "$active_run_root" "$local_start_branch" "$local_start_reflog_count" "$local_start_issue_worktrees")"
  else
    codex_exit_code="$?"
    set_recovery_prompt "codex_exec" "$codex_exit_code"
    active_run_root="$(recover_run_root_after_failed_codex_run "$active_run_root" "$local_start_branch" "$local_start_reflog_count" "$local_start_issue_worktrees")"
    echo "[ralph] Codex run $iteration failed with status $codex_exit_code; retrying with recovery instructions"
    advance_iteration
    continue
  fi

  echo "[ralph] starting auto-review for run $iteration in $active_run_root"
  if run_auto_review "$active_run_root"; then
    :
  else
    review_exit_code="$?"
    set_recovery_prompt "auto_review" "$review_exit_code"
    echo "[ralph] auto-review for run $iteration failed with status $review_exit_code; retrying with recovery instructions"
    advance_iteration
    continue
  fi

  recovery_prompt=""
  completed_runs=$((completed_runs + 1))

  if [[ "$max_runs" -gt 0 && "$completed_runs" -ge "$max_runs" ]]; then
    echo "[ralph] reached RALPH_MAX_RUNS=$max_runs"
    break
  fi

  advance_iteration
done
