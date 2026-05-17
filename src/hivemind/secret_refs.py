from __future__ import annotations

BROKER_SECRET_REF_SCHEME = "secret"  # nosec B105
BROKER_SECRET_REF_ERROR = "secret:// refs are broker-generated; provide secret_value for broker-managed storage"  # nosec B105
ALLOWED_SECRET_REF_SCHEMES = ("env", "file", "vault", "oauth", BROKER_SECRET_REF_SCHEME)
SECRET_REF_ERROR = "secret_ref must use env://, file://, vault://, oauth://, or secret://"  # nosec B105


def validate_secret_ref(secret_ref: str) -> str:
    scheme, separator, target = secret_ref.partition("://")
    if separator != "://" or scheme not in ALLOWED_SECRET_REF_SCHEMES or not target:
        raise ValueError(SECRET_REF_ERROR)
    return secret_ref


def validate_external_secret_ref(secret_ref: str) -> str:
    secret_ref = validate_secret_ref(secret_ref)
    scheme, _, _ = secret_ref.partition("://")
    if scheme == BROKER_SECRET_REF_SCHEME:
        raise ValueError(BROKER_SECRET_REF_ERROR)
    return secret_ref


def preview_secret_ref(secret_ref: str | None) -> str | None:
    if not secret_ref:
        return None
    scheme, _, rest = secret_ref.partition("://")
    return f"{scheme}://{rest[:3]}..." if rest else f"{scheme}://..."
