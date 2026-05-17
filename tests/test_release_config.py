from __future__ import annotations

from fnmatch import fnmatch
import json
from pathlib import Path
import shlex


def require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def require_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_false(condition: bool, message: str) -> None:
    if condition:
        raise AssertionError(message)


def dockerignore_patterns(repo_root: Path) -> list[tuple[str, bool]]:
    patterns: list[tuple[str, bool]] = []
    for line in (repo_root / ".dockerignore").read_text(encoding="utf-8").splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        patterns.append((pattern[1:] if negated else pattern, negated))
    return patterns


def dockerignore_matches(path: str, patterns: list[tuple[str, bool]]) -> bool:
    ignored = False
    normalized_path = path.strip("/")
    for pattern, negated in patterns:
        normalized_pattern = pattern.strip("/")
        if pattern.endswith("/"):
            matched = normalized_path == normalized_pattern or normalized_path.startswith(f"{normalized_pattern}/")
        elif "/" in normalized_pattern:
            matched = normalized_path == normalized_pattern or fnmatch(normalized_path, normalized_pattern)
        else:
            matched = any(fnmatch(part, normalized_pattern) for part in normalized_path.split("/"))
        if matched:
            ignored = not negated
    return ignored


def dockerfile_copy_sources(repo_root: Path) -> list[str]:
    sources: list[str] = []
    for line in (repo_root / "Dockerfile").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = shlex.split(stripped)
        args = parts[1:]
        while args and args[0].startswith("--"):
            args = args[1:]
        sources.extend(source.rstrip("/") for source in args[:-1])
    return sources


def test_semantic_release_config_skips_npm_publish_for_container_app() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    config = json.loads((repo_root / ".releaserc.json").read_text(encoding="utf-8"))

    require_equal(config["branches"], ["main"], "semantic-release should only publish from main")
    require_equal(config["tagFormat"], "v${version}", "semantic-release should create version tags")

    plugin_names = [plugin[0] if isinstance(plugin, list) else plugin for plugin in config["plugins"]]
    require_true("@semantic-release/commit-analyzer" in plugin_names, "release config should analyze commits")
    require_true(
        "@semantic-release/release-notes-generator" in plugin_names,
        "release config should generate release notes",
    )
    require_true("@semantic-release/github" in plugin_names, "release config should publish GitHub releases")
    require_false("@semantic-release/npm" in plugin_names, "container app should not publish to npm")

    github_plugin = next(
        plugin for plugin in config["plugins"] if isinstance(plugin, list) and plugin[0] == "@semantic-release/github"
    )
    github_options = github_plugin[1]
    require_false(github_options["successComment"], "release config should not mutate released issues")
    require_false(github_options["failComment"], "release config should not open failure issues")
    require_false(github_options["labels"], "release config should not add failure labels")
    require_false(github_options["releasedLabels"], "release config should not add released labels")


def test_dockerignore_keeps_local_state_out_of_build_context() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    patterns = dockerignore_patterns(repo_root)

    ignored_paths = [
        ".git/config",
        ".env",
        ".env.local",
        ".data/hivemind.db",
        "scratch.db",
        "scratch.db-wal",
        "scratch.db-shm",
        "scratch.sqlite",
        "scratch.sqlite-wal",
        "scratch.sqlite3",
        "scratch.sqlite3-shm",
        ".agents/runtime/worker/state.json",
        ".agents/skills/codex-review/SKILL.md",
        "__pycache__/module.pyc",
        "tests/__pycache__/test_api.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".ruff_cache/0.14.6/cache",
        ".venv/bin/python",
        ".direnv/python-3.12/bin/python",
        "node_modules/example/package.json",
        ".qlty/sources/example/tool",
        "tmp/repro.log",
    ]
    for path in ignored_paths:
        require_true(dockerignore_matches(path, patterns), f"{path} should stay out of Docker build context")

    for source in dockerfile_copy_sources(repo_root):
        require_false(dockerignore_matches(source, patterns), f"Dockerfile COPY source {source} must remain in context")
