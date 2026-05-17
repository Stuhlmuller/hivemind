#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
tools_file="$script_dir/TOOLS.md"
flake_file="$repo_root/flake.nix"

ensure_codex_ready() {
  local loop_label="$1"

  if command -v codex >/dev/null 2>&1; then
    return
  fi

  echo "[$loop_label] codex is required in PATH" >&2
  exit 1
}

ensure_git_ready() {
  local loop_label="$1"

  if command -v git >/dev/null 2>&1; then
    return
  fi

  echo "[$loop_label] git is required in PATH" >&2
  exit 1
}

ensure_github_ready() {
  local loop_label="$1"

  if ! command -v gh >/dev/null 2>&1; then
    echo "[$loop_label] gh is required in PATH" >&2
    exit 1
  fi

  if ! gh auth status; then
    echo "[$loop_label] gh authentication is required; run 'gh auth login -h github.com' and retry" >&2
    exit 1
  fi

  if ! gh issue list --state all --limit 1 >/dev/null 2>&1; then
    echo "[$loop_label] gh must be able to read repository issues before this loop can run" >&2
    exit 1
  fi
}

ensure_not_nested_codex() {
  local loop_label="$1"

  if [[ -z "${CODEX_SANDBOX:-}" ]]; then
    return
  fi

  echo "[$loop_label] nested Codex runs are not supported inside a Codex sandbox (${CODEX_SANDBOX})." >&2
  echo "[$loop_label] run this loop from a normal terminal session instead." >&2
  exit 1
}

ensure_bootstrap_files() {
  local loop_label="$1"

  if [[ ! -f "$tools_file" ]]; then
    echo "[$loop_label] missing bootstrap tool manifest: $tools_file" >&2
    exit 1
  fi

  if [[ ! -f "$flake_file" ]]; then
    echo "[$loop_label] missing flake file: $flake_file" >&2
    exit 1
  fi
}

canonicalize_path() {
  local target_path="$1"
  (
    cd "$target_path"
    pwd -P
  )
}

default_branch_name() {
  local ref

  ref="$(git -C "$repo_root" symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [[ -z "$ref" ]]; then
    echo "[swarm] unable to determine the default branch from origin/HEAD" >&2
    exit 1
  fi

  printf '%s\n' "${ref#refs/remotes/origin/}"
}

worktree_start_ref() {
  local default_branch

  default_branch="$(default_branch_name)"
  if git -C "$repo_root" rev-parse --verify "origin/$default_branch" >/dev/null 2>&1; then
    printf '%s\n' "origin/$default_branch"
    return
  fi

  printf '%s\n' "$default_branch"
}

current_branch_display() {
  local run_root="$1"
  local branch
  local revision

  branch="$(git -C "$run_root" branch --show-current)"
  if [[ -n "$branch" ]]; then
    printf '%s\n' "$branch"
    return
  fi

  revision="$(git -C "$run_root" rev-parse --short HEAD 2>/dev/null || true)"
  if [[ -z "$revision" ]]; then
    revision="unknown"
  fi

  printf 'detached@%s\n' "$revision"
}

worktree_exists_for_path() {
  local target_path
  local line

  target_path="$(canonicalize_path "$1")"

  while IFS= read -r line; do
    case "$line" in
      worktree\ *)
        if [[ "$(canonicalize_path "${line#worktree }")" == "$target_path" ]]; then
          return 0
        fi
        ;;
    esac
  done <<<"$(git -C "$repo_root" worktree list --porcelain)"

  return 1
}

ensure_detached_worktree() {
  local target_path="$1"
  local start_ref

  if [[ -d "$target_path" ]]; then
    if worktree_exists_for_path "$target_path"; then
      return
    fi

    echo "[swarm] path exists but is not a registered git worktree: $target_path" >&2
    exit 1
  fi

  mkdir -p "$(dirname "$target_path")"
  start_ref="$(worktree_start_ref)"
  git -C "$repo_root" worktree add --detach "$target_path" "$start_ref" >/dev/null
}

pid_is_running() {
  local pid="$1"

  if [[ -z "$pid" ]]; then
    return 1
  fi

  kill -0 "$pid" >/dev/null 2>&1
}

run_codex_exec() {
  local run_root="$1"
  local prompt_file="$2"
  local shared_prompt_file=""
  local prompt_text
  local -a cmd

  if [[ "$#" -ge 3 ]]; then
    shared_prompt_file="$3"
    shift 3
  else
    shift 2
  fi

  if [[ -n "$shared_prompt_file" ]]; then
    prompt_text="$(<"$shared_prompt_file")"$'\n\n'"$(<"$prompt_file")"
  else
    prompt_text="$(<"$prompt_file")"
  fi
  cmd=(codex exec -C "$run_root" -s danger-full-access)

  if [[ "$#" -gt 0 ]]; then
    cmd+=("$@")
  fi

  cmd+=("$prompt_text")
  "${cmd[@]}"
}

run_codex_review() {
  local run_root="$1"
  local review_prompt="$2"

  (
    cd "$run_root"
    codex review "$review_prompt"
  )
}

role_focus_area() {
  local role_kind="$1"
  local slot_index="$2"
  local focus_slot

  focus_slot="$(( ((slot_index - 1) % 3) + 1 ))"

  case "$role_kind:$focus_slot" in
    reviewer:1)
      printf '%s\n' "security, correctness, tests, and regression risk"
      ;;
    reviewer:2)
      printf '%s\n' "docs, onboarding, CI, and release readiness"
      ;;
    reviewer:3)
      printf '%s\n' "operations, observability, deployment, and backlog-duplication checks"
      ;;
    feature-requester:1)
      printf '%s\n' "core product workflows, task and agent UX, and operator ergonomics"
      ;;
    feature-requester:2)
      printf '%s\n' "developer experience, automation, and release workflow improvements"
      ;;
    feature-requester:3)
      printf '%s\n' "security posture, credential UX, and self-hosted administration gaps"
      ;;
    scout:1)
      printf '%s\n' "setup, login, and credential-management flows"
      ;;
    scout:2)
      printf '%s\n' "task, swarm, PR, and workflow visibility on shipped branches"
      ;;
    scout:3)
      printf '%s\n' "docs, onboarding, and release-operator paths"
      ;;
    *)
      printf '%s\n' "general backlog coverage"
      ;;
  esac
}

default_loop_prompt_preamble() {
  local role_kind="$1"
  local loop_label="$2"
  local slot_index="$3"
  local slot_count="$4"
  local focus_area

  case "$role_kind" in
    reviewer)
      focus_area="$(role_focus_area "$role_kind" "$slot_index")"
      cat <<EOF
## Lane Assignment

You are \`$loop_label\`, reviewer lane $slot_index of $slot_count.
Primary focus: $focus_area.
Work this focus first to reduce overlap with the other reviewer lanes. File only grounded bug, regression, reliability, docs, release, or operator issues that are not already covered.
EOF
      ;;
    feature-requester)
      focus_area="$(role_focus_area "$role_kind" "$slot_index")"
      cat <<EOF
## Lane Assignment

You are \`$loop_label\`, feature-requester lane $slot_index of $slot_count.
Primary focus: $focus_area.
Work this focus first to reduce overlap with the other feature-requester lanes. Draft only concrete feature issues with repo evidence; do not start implementation.
EOF
      ;;
    scout)
      focus_area="$(role_focus_area "$role_kind" "$slot_index")"
      cat <<EOF
## Lane Assignment

You are \`$loop_label\`, scout lane $slot_index of $slot_count.
Primary shipped-behavior focus: $focus_area.
Use the Codex browser tool only on the default branch and only to validate shipped behavior that can turn into concrete issues. Do not use it for speculative feature ideation.
EOF
      ;;
    worker)
      cat <<EOF
## Lane Assignment

You are \`$loop_label\`, worker lane $slot_index of $slot_count.
Only pick issues whose lane matches this formula: \`((issue_number - 1) % $slot_count) + 1 == $slot_index\`.
If no eligible issue in your lane is ready, stay idle rather than stealing another worker lane's issue.
EOF
      ;;
    pr-shepherd)
      cat <<EOF
## Lane Assignment

You are \`$loop_label\`, PR shepherd lane $slot_index of $slot_count.
Only take primary ownership of open pull requests whose lane matches this formula: \`((pr_number - 1) % $slot_count) + 1 == $slot_index\`.
If another worker or shepherd worktree visibly owns the branch, skip it even if the PR number matches your lane.
EOF
      ;;
    *)
      return 1
      ;;
  esac
}
