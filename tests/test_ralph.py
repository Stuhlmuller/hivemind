from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_executable(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents))
    path.chmod(0o755)


def init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Ralph Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "ralph@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("ralph test repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )


def create_temp_ralph_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    agents_dir = repo / ".agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "ralph.sh").write_text((REPO_ROOT / ".agents/ralph.sh").read_text())
    (agents_dir / "ralph.sh").chmod(0o755)
    (agents_dir / "PROMPT.md").write_text((REPO_ROOT / ".agents/PROMPT.md").read_text())
    init_git_repo(repo)
    return repo


def install_gh_stub(bin_dir: Path) -> None:
    write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
        set -euo pipefail

        case "${1:-}" in
          auth)
            if [[ "${2:-}" == "status" ]]; then
              printf 'github.com\\n'
              exit 0
            fi
            ;;
          issue)
            if [[ "${2:-}" == "list" ]]; then
              printf '123\\tOPEN\\tStub issue\\t\\t2026-01-01T00:00:00Z\\n'
              exit 0
            fi
            ;;
        esac

        echo "unexpected gh invocation: $*" >&2
        exit 64
        """,
    )


def install_codex_stub(bin_dir: Path) -> None:
    write_executable(
        bin_dir / "codex",
        """#!/usr/bin/env bash
        set -euo pipefail

        log_dir="${RALPH_TEST_LOG_DIR:?}"
        mkdir -p "$log_dir"

        mode="${1:-}"
        shift || true

        next_count() {
          local name="$1"
          local count_file="$log_dir/${name}_count"
          local count=1
          if [[ -f "$count_file" ]]; then
            count=$(( $(cat "$count_file") + 1 ))
          fi
          printf '%s' "$count" > "$count_file"
          printf '%s' "$count"
        }

        last_arg() {
          local value=""
          for value in "$@"; do
            :
          done
          printf '%s' "$value"
        }

        case "$mode" in
          exec)
            count="$(next_count exec)"
            prompt="$(last_arg "$@")"
            printf '%s' "$prompt" > "$log_dir/exec_prompt_${count}.txt"
            printf '%s\\n' "$*" > "$log_dir/exec_args_${count}.txt"

            repo=""
            saw_network_flag=0
            args=( "$@" )
            index=0
            while [[ "$index" -lt "${#args[@]}" ]]; do
              case "${args[$index]}" in
                -C)
                  index=$((index + 1))
                  repo="${args[$index]}"
                  ;;
                -s)
                  index=$((index + 1))
                  if [[ "${args[$index]}" == "danger-full-access" ]]; then
                    saw_network_flag=1
                  fi
                  ;;
              esac
              index=$((index + 1))
            done

            if [[ "$saw_network_flag" -ne 1 ]]; then
              echo "missing danger-full-access" >&2
              exit 97
            fi

            scenario="${RALPH_TEST_EXEC_SCENARIO:-success_issue_branch}"
            case "$scenario" in
              fail_then_issue_branch)
                if [[ "$count" -eq 1 ]]; then
                  echo "simulated exec failure" >&2
                  exit 23
                fi
                git -C "$repo" switch issue-123-recovery >/dev/null 2>&1 || git -C "$repo" switch -c issue-123-recovery >/dev/null 2>&1
                ;;
              no_issue_branch)
                ;;
              success_issue_branch)
                git -C "$repo" switch issue-123-success >/dev/null 2>&1 || git -C "$repo" switch -c issue-123-success >/dev/null 2>&1
                ;;
              *)
                echo "unexpected exec scenario: $scenario" >&2
                exit 98
                ;;
            esac
            ;;
          review)
            count="$(next_count review)"
            prompt="$(last_arg "$@")"
            printf '%s' "$prompt" > "$log_dir/review_prompt_${count}.txt"
            scenario="${RALPH_TEST_REVIEW_SCENARIO:-success}"
            case "$scenario" in
              fail_once)
                if [[ "$count" -eq 1 ]]; then
                  echo "simulated review failure" >&2
                  exit 29
                fi
                ;;
              success)
                ;;
              *)
                echo "unexpected review scenario: $scenario" >&2
                exit 99
                ;;
            esac
            ;;
          *)
            echo "unexpected codex mode: $mode" >&2
            exit 65
            ;;
        esac
        """,
    )


def run_ralph(repo: Path, tmp_path: Path, *, exec_scenario: str, review_scenario: str = "success") -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_gh_stub(bin_dir)
    install_codex_stub(bin_dir)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "CODEX_SANDBOX": "",
            "RALPH_MAX_RUNS": "1",
            "RALPH_SLEEP_SECONDS": "0",
            "RALPH_TEST_EXEC_SCENARIO": exec_scenario,
            "RALPH_TEST_REVIEW_SCENARIO": review_scenario,
            "RALPH_TEST_LOG_DIR": str(tmp_path / "logs"),
        }
    )
    return subprocess.run(
        [str(repo / ".agents/ralph.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class RalphScriptTests(unittest.TestCase):
    def test_ralph_retries_failed_codex_run_with_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            repo = create_temp_ralph_repo(tmp_path)

            result = run_ralph(repo, tmp_path, exec_scenario="fail_then_issue_branch")

            self.assertEqual(result.returncode, 0, result.stderr)
            first_prompt = (tmp_path / "logs/exec_prompt_1.txt").read_text()
            second_prompt = (tmp_path / "logs/exec_prompt_2.txt").read_text()
            first_args = (tmp_path / "logs/exec_args_1.txt").read_text()

            self.assertNotIn("## Ralph Recovery Instruction", first_prompt)
            self.assertIn("## Ralph Recovery Instruction", second_prompt)
            self.assertIn("Failure stage: codex exec", second_prompt)
            self.assertIn("Exit status: 23", second_prompt)
            self.assertIn("-s danger-full-access", first_args)
            self.assertIn("retrying with recovery instructions", result.stdout)

    def test_ralph_fails_when_no_issue_branch_is_opened(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            repo = create_temp_ralph_repo(tmp_path)

            result = run_ralph(repo, tmp_path, exec_scenario="no_issue_branch")

            combined_output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not switch onto an issue branch", combined_output)

    def test_ralph_retries_failed_auto_review_with_recovery_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            repo = create_temp_ralph_repo(tmp_path)

            result = run_ralph(
                repo,
                tmp_path,
                exec_scenario="success_issue_branch",
                review_scenario="fail_once",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            second_prompt = (tmp_path / "logs/exec_prompt_2.txt").read_text()

            self.assertIn("## Ralph Recovery Instruction", second_prompt)
            self.assertIn("Failure stage: codex review", second_prompt)
            self.assertIn("Exit status: 29", second_prompt)
            self.assertIn("auto-review for run 1 failed with status 29", result.stdout)

    def test_ralph_accepts_successful_issue_branch_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            repo = create_temp_ralph_repo(tmp_path)

            result = run_ralph(repo, tmp_path, exec_scenario="success_issue_branch")

            self.assertEqual(result.returncode, 0, result.stderr)
            current_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(current_branch, "issue-123-success")


if __name__ == "__main__":
    unittest.main()
