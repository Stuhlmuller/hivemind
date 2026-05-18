#!/usr/bin/env python3
"""
Resolve one or more GitHub PR review threads by GraphQL node ID.

Use thread IDs from scripts/fetch_comments.py output:

  python resolve_threads.py PRRT_kwDOExample...
  python resolve_threads.py PRRT_one PRRT_two

Requires `gh auth login` and a token with permission to resolve review threads.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

MUTATION = """\
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""


def _run(cmd: list[str], stdin: str | None = None) -> str:
    process = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if process.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{process.stderr}")
    return process.stdout


def _run_json(cmd: list[str], stdin: str | None = None) -> dict[str, Any]:
    out = _run(cmd, stdin=stdin)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Failed to parse JSON from command output: {exc}\nRaw:\n{out}"
        ) from exc


def _ensure_gh_authenticated() -> None:
    try:
        _run(["gh", "auth", "status"])
    except RuntimeError:
        print("run `gh auth login` to authenticate the GitHub CLI", file=sys.stderr)
        raise RuntimeError(
            "gh auth status failed; run `gh auth login` to authenticate the GitHub CLI"
        ) from None


def resolve_thread(thread_id: str) -> dict[str, Any]:
    payload = _run_json(
        [
            "gh",
            "api",
            "graphql",
            "-F",
            "query=@-",
            "-F",
            f"threadId={thread_id}",
        ],
        stdin=MUTATION,
    )

    if "errors" in payload and payload["errors"]:
        raise RuntimeError(
            f"GitHub GraphQL errors:\n{json.dumps(payload['errors'], indent=2)}"
        )

    thread = (
        payload.get("data", {})
        .get("resolveReviewThread", {})
        .get("thread")
    )
    if not thread or not thread.get("isResolved"):
        raise RuntimeError(f"GitHub did not confirm resolution for {thread_id}")
    return thread


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve GitHub PR review threads by GraphQL node ID."
    )
    parser.add_argument("thread_ids", nargs="+", help="Review thread GraphQL node IDs")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the thread IDs that would be resolved without calling GitHub.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.dry_run:
        print(json.dumps({"would_resolve": args.thread_ids}, indent=2))
        return

    _ensure_gh_authenticated()
    resolved = [resolve_thread(thread_id) for thread_id in args.thread_ids]
    print(json.dumps({"resolved": resolved}, indent=2))


if __name__ == "__main__":
    main()
