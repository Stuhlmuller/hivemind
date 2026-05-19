from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn

from hivemind.store import HivemindStore, StoreError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hivemind")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the Hivemind API server")
    serve.add_argument("--host", default="0.0.0.0")  # nosec B104 - container entrypoint must be reachable
    serve.add_argument("--port", default=8000, type=int)

    backup = subparsers.add_parser("backup", help="write a logical JSON backup bundle")
    backup.add_argument("path", help="output path or - for stdout")

    restore = subparsers.add_parser("restore", help="restore a logical JSON backup bundle")
    restore.add_argument("path", help="input path or - for stdin")

    admin = subparsers.add_parser("admin", help="offline local admin maintenance")
    admin_subparsers = admin.add_subparsers(dest="admin_command")
    reset_password = admin_subparsers.add_parser(
        "reset-password",
        help="reset an existing local admin password from an offline operator shell",
    )
    reset_password.add_argument("--username", required=True, help="existing local admin username")
    reset_password.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the new password from stdin instead of an interactive prompt",
    )
    return parser


def write_json_document(path: str, payload: dict[str, Any]) -> None:
    if path == "-":
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_json_document(path: str) -> dict[str, Any]:
    if path == "-":
        payload = json.load(sys.stdin)
    else:
        with Path(path).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    if not isinstance(payload, dict):
        raise StoreError("backup bundle must be a JSON object")
    return payload


def emit_summary(action: str, path: str, summary: dict[str, int]) -> None:
    counts = ", ".join(f"{table}={count}" for table, count in summary.items())
    print(f"{action} {path}: {counts}", file=sys.stderr)


def read_admin_recovery_password(*, password_stdin: bool) -> str:
    if password_stdin:
        return sys.stdin.readline().rstrip("\n")
    password = getpass.getpass("New admin password: ")
    confirmation = getpass.getpass("Confirm admin password: ")
    if password != confirmation:
        raise StoreError("password confirmation does not match")
    return password


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_args if raw_args else ["serve"])

    if args.command == "serve":
        uvicorn.run("hivemind.api:create_app", factory=True, host=args.host, port=args.port)
        return 0

    try:
        if args.command == "backup":
            store = HivemindStore.from_env(require_existing=True)
            bundle = store.export_backup_bundle()
            write_json_document(args.path, bundle)
            emit_summary("backed up", args.path, bundle["summary"])
            return 0
        if args.command == "restore":
            bundle = read_json_document(args.path)
            store = HivemindStore.from_env()
            summary = store.restore_backup_bundle(bundle)
            emit_summary("restored", args.path, summary)
            return 0
        if args.command == "admin" and args.admin_command == "reset-password":
            store = HivemindStore.from_env(require_existing=True)
            password = read_admin_recovery_password(password_stdin=args.password_stdin)
            user = store.reset_admin_password(args.username, password)
            print(f"reset local admin password for {user['username']}; active sessions revoked", file=sys.stderr)
            return 0
    except (OSError, json.JSONDecodeError, sqlite3.Error, StoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
