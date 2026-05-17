from __future__ import annotations

import argparse
import json
import os
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
            store = HivemindStore.from_env()
            bundle = read_json_document(args.path)
            summary = store.restore_backup_bundle(bundle)
            emit_summary("restored", args.path, summary)
            return 0
    except (OSError, json.JSONDecodeError, StoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
