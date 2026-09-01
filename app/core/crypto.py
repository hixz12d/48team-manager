"""Token encryption. Key rotation must not reuse the session secret."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings, load_settings


class TokenCipher:
    def __init__(self, key: str):
        hashed = hashlib.sha256((key or "").encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(hashed))

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt((token or "").encode("utf-8")).decode("utf-8")

    def decrypt(self, encrypted_token: str) -> str:
        try:
            return self._fernet.decrypt((encrypted_token or "").encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            raise ValueError("token decrypt failed") from exc


def token_cipher(settings: Settings | None = None) -> TokenCipher:
    settings = settings or load_settings()
    return TokenCipher(settings.effective_encryption_key)
