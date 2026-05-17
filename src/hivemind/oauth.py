from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from cryptography.fernet import Fernet


class OAuthConfigurationError(ValueError):
    pass


def split_scopes(value: str | None, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    scopes = tuple(part for part in (value or "").split() if part)
    return scopes or default


def build_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return verifier, challenge


@dataclass(frozen=True)
class OAuthProviderConfig:
    id: str
    label: str
    credential_provider: str
    authorize_url: str | None
    token_url: str | None
    client_id: str | None
    client_secret: str | None
    scopes: tuple[str, ...]
    supports_pkce: bool = True

    def availability(self, *, has_secret_store: bool) -> tuple[bool, str | None]:
        if not has_secret_store:
            return False, "Set HIVEMIND_SECRETS_KEY to enable broker-side OAuth token storage."
        missing = []
        if not self.authorize_url:
            missing.append("authorize_url")
        if not self.token_url:
            missing.append("token_url")
        if not self.client_id:
            missing.append("client_id")
        if missing:
            fields = ", ".join(missing)
            return False, f"Missing OAuth configuration: {fields}."
        return True, None

    def public_view(self, *, has_secret_store: bool) -> dict[str, Any]:
        available, reason = self.availability(has_secret_store=has_secret_store)
        return {
            "id": self.id,
            "label": self.label,
            "credential_provider": self.credential_provider,
            "available": available,
            "reason": reason,
            "scopes": list(self.scopes),
            "supports_pkce": self.supports_pkce,
        }

    def build_authorize_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
    ) -> str:
        available, reason = self.availability(has_secret_store=True)
        if not available:
            raise OAuthConfigurationError(reason or f"OAuth provider {self.id} is unavailable")
        query = {
            "response_type": "code",
            "client_id": self.client_id or "",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if self.scopes:
            query["scope"] = " ".join(self.scopes)
        if self.supports_pkce:
            query["code_challenge"] = code_challenge
            query["code_challenge_method"] = "S256"
        separator = "&" if "?" in (self.authorize_url or "") else "?"
        return f"{self.authorize_url}{separator}{urlencode(query)}"

    def build_token_payload(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict[str, str]:
        available, reason = self.availability(has_secret_store=True)
        if not available:
            raise OAuthConfigurationError(reason or f"OAuth provider {self.id} is unavailable")
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id or "",
        }
        if self.supports_pkce:
            payload["code_verifier"] = code_verifier
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        return payload


class SecretBox:
    def __init__(self, key_material: str) -> None:
        normalized = key_material.strip()
        if not normalized:
            raise OAuthConfigurationError("HIVEMIND_SECRETS_KEY must not be empty")
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(normalized.encode("utf-8")).digest())
        self._fernet = Fernet(derived_key)

    @classmethod
    def from_env(cls) -> "SecretBox | None":
        value = os.getenv("HIVEMIND_SECRETS_KEY")
        if not value:
            return None
        return cls(value)

    def encrypt_text(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt_text(self, token: str) -> str:
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")

    def encrypt_json(self, payload: dict[str, Any]) -> str:
        serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return self.encrypt_text(serialized)

    def decrypt_json(self, token: str) -> dict[str, Any]:
        return json.loads(self.decrypt_text(token))


def load_oauth_providers_from_env() -> dict[str, OAuthProviderConfig]:
    return {
        "codex": OAuthProviderConfig(
            id="codex",
            label="Codex subscription",
            credential_provider="codex",
            authorize_url=os.getenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL"),
            token_url=os.getenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL"),
            client_id=os.getenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID"),
            client_secret=os.getenv("HIVEMIND_OAUTH_CODEX_CLIENT_SECRET"),
            scopes=split_scopes(
                os.getenv("HIVEMIND_OAUTH_CODEX_SCOPES"),
                default=("openid", "profile", "email", "offline_access"),
            ),
        )
    }
