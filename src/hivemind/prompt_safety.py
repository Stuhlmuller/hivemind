from __future__ import annotations

import re

REDACTED_PROMPT_VALUE = "[redacted]"
UNSAFE_PROMPT_ERROR = (
    "system_prompt contains secret-like material; remove credential refs, tokens, "
    "password assignments, or private-key blocks and configure credentials through the broker"
)

SECRET_REF_TEXT_PATTERN = re.compile(r"\b(?:env|file|vault|oauth|secret)://[^\s\"'<>),\]}]+")
PRIVATE_KEY_BLOCK_PATTERN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)
BEARER_TOKEN_PATTERN = re.compile(r"\bBearer\s+\S{12,}", re.IGNORECASE)
PROVIDER_TOKEN_PATTERN = re.compile(
    r"\b(?:"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"hv[lp]_[A-Za-z0-9_-]{16,}"
    r")\b"
)
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:"
    r"access[_-]?token|api[_-]?key|authorization|bearer[_-]?token|"
    r"client[_-]?secret|password|refresh[_-]?token|secret(?:[_-]?(?:key|value))?|token"
    r")\b\s*[:=]\s*[\"']?\S{4,}",
    re.IGNORECASE,
)

PROMPT_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("credential reference", SECRET_REF_TEXT_PATTERN),
    ("private-key block", PRIVATE_KEY_BLOCK_PATTERN),
    ("bearer token", BEARER_TOKEN_PATTERN),
    ("provider token", PROVIDER_TOKEN_PATTERN),
    ("secret assignment", SENSITIVE_ASSIGNMENT_PATTERN),
)


def prompt_secret_findings(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    findings: list[str] = []
    for reason, pattern in PROMPT_SECRET_PATTERNS:
        if pattern.search(value):
            findings.append(reason)
    return tuple(findings)


def prompt_contains_secret_like_material(value: str | None) -> bool:
    return bool(prompt_secret_findings(value))


def validate_prompt_like_text(value: str | None, *, field_name: str = "system_prompt") -> str:
    text = value or ""
    if prompt_contains_secret_like_material(text):
        raise ValueError(UNSAFE_PROMPT_ERROR.replace("system_prompt", field_name, 1))
    return text


def redact_prompt_like_text(value: str | None) -> str:
    text = value or ""
    if prompt_contains_secret_like_material(text):
        return REDACTED_PROMPT_VALUE
    return text
