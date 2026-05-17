from __future__ import annotations


def preview_secret_ref(secret_ref: str | None) -> str | None:
    if not secret_ref:
        return None
    scheme, _, rest = secret_ref.partition("://")
    return f"{scheme}://{rest[:3]}..." if rest else f"{scheme}://..."
