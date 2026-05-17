from __future__ import annotations

from pathlib import Path


def test_readme_and_frontend_avoid_generic_ai_slop_terms() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    checked_files = [
        repo_root / "README.md",
        repo_root / "src/hivemind/static/index.html",
        repo_root / "src/hivemind/static/app.js",
    ]
    banned_terms = [
        "action-capable",
        "ai-powered",
        "bee-themed",
        "enterprise-grade",
        "hivemind init",
        "installation flows",
        "live audit stream",
        "mission control",
        "powerful",
        "production ready",
        "refresh actions",
        "robust",
        "seamless",
        "secure by design",
        "supercharge",
        "unlock",
        ">execute<",
    ]

    findings = []
    for file_path in checked_files:
        text = file_path.read_text(encoding="utf-8").lower()
        for term in banned_terms:
            if term in text:
                findings.append(f"{file_path.relative_to(repo_root)} contains {term!r}")

    if findings:
        raise AssertionError(f"README and frontend copy contains generic AI/SaaS terms: {findings}")


def test_frontend_oauth_defaults_are_provider_neutral() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    frontend_text = "\n".join(
        [
            (repo_root / "src/hivemind/static/index.html").read_text(encoding="utf-8"),
            (repo_root / "src/hivemind/static/app.js").read_text(encoding="utf-8"),
        ]
    ).lower()
    provider_specific_defaults = [
        "codex subscription",
        "codex-oauth",
        'payload.provider = "codex"',
        "oauth://codex",
        "delegate_code",
        "review_code",
    ]

    findings = [term for term in provider_specific_defaults if term in frontend_text]

    if findings:
        raise AssertionError(f"frontend OAuth defaults should be provider-neutral: {findings}")
