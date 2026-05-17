from __future__ import annotations

ALLOWED_SECRET_REF_SCHEMES = ("env", "file", "vault", "oauth")
SECRET_REF_ERROR = "secret_ref must use env://, file://, vault://, or oauth://"


def validate_secret_ref(secret_ref: str) -> str:
    scheme, separator, target = secret_ref.partition("://")
    if separator != "://" or scheme not in ALLOWED_SECRET_REF_SCHEMES or not target:
        raise ValueError(SECRET_REF_ERROR)
    return secret_ref


def preview_secret_ref(secret_ref: str | None) -> str | None:
    if not secret_ref:
        return None
    scheme, _, rest = secret_ref.partition("://")
    return f"{scheme}://{rest[:3]}..." if rest else f"{scheme}://..."
