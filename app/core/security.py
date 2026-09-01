"""Password hashing and session helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging

import bcrypt

logger = logging.getLogger(__name__)

_BCRYPT_MAX_INPUT_BYTES = 72


def prepare_for_bcrypt(password: str) -> bytes:
    password_bytes = password.encode("utf-8")
    if len(password_bytes) <= _BCRYPT_MAX_INPUT_BYTES:
        return password_bytes
    digest = hashlib.sha256(password_bytes).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(prepare_for_bcrypt(password), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        hashed_bytes = hashed_password.encode("utf-8")
        if bcrypt.checkpw(prepare_for_bcrypt(password), hashed_bytes):
            return True
        password_bytes = password.encode("utf-8")
        if len(password_bytes) > _BCRYPT_MAX_INPUT_BYTES:
            try:
                return bcrypt.checkpw(password_bytes[:_BCRYPT_MAX_INPUT_BYTES], hashed_bytes)
            except ValueError:
                return False
        return False
    except Exception:
        logger.exception("password verification failed")
        return False


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
