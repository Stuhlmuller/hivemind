from __future__ import annotations

import json
from pathlib import Path


def require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def require_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_false(condition: bool, message: str) -> None:
    if condition:
        raise AssertionError(message)


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
