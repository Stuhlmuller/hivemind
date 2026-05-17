#!/usr/bin/env bash

if [ -z "${BASH_VERSION:-}" ]; then
  exec /usr/bin/env bash "$0" "$@"
fi

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
prompt_file="$script_dir/PROMPT.md"
tools_file="$script_dir/TOOLS.md"
flake_file="$repo_root/flake.nix"

max_runs="${RALPH_MAX_RUNS:-0}"
sleep_seconds="${RALPH_SLEEP_SECONDS:-0}"
review_prompt="${RALPH_REVIEW_PROMPT:-Review the current uncommitted changes. Prioritize correctness bugs, regressions, and missing tests.}"
iteration=1

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

default_branch_name() {
  local ref

  ref="$(git symbolic-ref --quiet refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [[ -z "$ref" ]]; then
    echo "[ralph] unable to determine the default branch from origin/HEAD" >&2
    exit 1
  fi

  printf '%s\n' "${ref#refs/remotes/origin/}"
}

current_branch_name() {
  local branch

  branch="$(git branch --show-current)"
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

head_reflog_count() {
  git reflog --format='%gs' | wc -l | tr -d '[:space:]'
}

reflog_includes_issue_branch_checkout() {
  local reflog_delta="$1"
  local entry

  if [[ "$reflog_delta" -le 0 ]]; then
    return 1
  fi

  while IFS= read -r entry; do
    if [[ "$entry" =~ ^checkout:\ moving\ from\ .+\ to\ issue-[0-9]+-[a-z0-9][a-z0-9-]*$ ]]; then
      return 0
    fi
  done <<<"$(git reflog --format='%gs' -n "$reflog_delta")"

  return 1
}

ensure_branch_context_before_run() {
  current_branch_name >/dev/null
}

ensure_issue_branch_activity_after_run() {
  local start_branch="$1"
  local start_reflog_count="$2"
  local default_branch end_branch end_reflog_count reflog_delta

  default_branch="$(default_branch_name)"
  end_branch="$(current_branch_name)"
  end_reflog_count="$(head_reflog_count)"
  reflog_delta=$((end_reflog_count - start_reflog_count))

  if [[ "$end_branch" != "$default_branch" ]] && ! is_issue_branch_name "$end_branch"; then
    echo "[ralph] Codex run ended on non-issue branch '$end_branch'; expected '$default_branch' or issue-<number>-<slug>" >&2
    exit 1
  fi

  if is_issue_branch_name "$end_branch"; then
    if [[ "$start_branch" == "$default_branch" && "$end_branch" == "$start_branch" ]]; then
      echo "[ralph] Codex run stayed on '$default_branch'; Ralph requires an issue branch before continuing" >&2
      exit 1
    fi
    return
  fi

  if reflog_includes_issue_branch_checkout "$reflog_delta"; then
    return
  fi

  echo "[ralph] Codex run did not switch onto an issue branch. Expected a checkout to issue-<number>-<slug> before returning to '$end_branch'" >&2
  exit 1
}

run_codex_exec() {
  local prompt_text
  local -a cmd

  prompt_text="$(<"$prompt_file")"
  # Ralph must be able to read issues, open PRs, and merge them via `gh`,
  # which requires network access inside the spawned Codex run.
  cmd=(codex exec -C "$repo_root" -s danger-full-access)

  if [[ "$#" -gt 0 ]]; then
    cmd+=("$@")
  fi

  cmd+=("$prompt_text")
  "${cmd[@]}"
}

run_auto_review() {
  (
    cd "$repo_root"
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

while :; do
  local_start_branch="$(current_branch_name)"
  local_start_reflog_count="$(head_reflog_count)"

  ensure_github_ready
  ensure_branch_context_before_run
  echo "[ralph] starting Codex run $iteration"
  run_codex_exec "$@"
  ensure_issue_branch_activity_after_run "$local_start_branch" "$local_start_reflog_count"

  echo "[ralph] starting auto-review for run $iteration"
  run_auto_review

  if [[ "$max_runs" -gt 0 && "$iteration" -ge "$max_runs" ]]; then
    echo "[ralph] reached RALPH_MAX_RUNS=$max_runs"
    break
  fi

  iteration=$((iteration + 1))

  if [[ "$sleep_seconds" != "0" ]]; then
    sleep "$sleep_seconds"
  fi
done
