# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from dotenv import load_dotenv

load_dotenv()

PASSWORD_SCHEME = "pbkdf2_sha256"
_PBKDF2_RAW = os.getenv("PASSWORD_HASH_ITERATIONS", "390000")
try:
    _parsed = int(_PBKDF2_RAW)
    if _parsed <= 0:
        raise ValueError("must be positive")
    PBKDF2_ITERATIONS = _parsed
except (ValueError, TypeError):
    PBKDF2_ITERATIONS = 390000
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def get_secret_key() -> str:
    secret_key = (os.getenv("SECRET_KEY") or "").strip()
    if not secret_key:
        raise RuntimeError("SECRET_KEY is required")
    return secret_key


def get_cookie_secure() -> bool:
    return (os.getenv("SESSION_COOKIE_SECURE") or "").strip().lower() in _TRUE_VALUES


def hash_password(password: str, *, salt: str | None = None) -> str:
    if password is None:
        raise ValueError("password is required")

    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        PBKDF2_ITERATIONS,
    )
    return f"{PASSWORD_SCHEME}${PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_value: str) -> bool:
    if password is None or not stored_value:
        return False

    # Reject unrecognized hash formats for security.
    # Only PBKDF2-SHA256 hashes are accepted.
    if not stored_value.startswith(f"{PASSWORD_SCHEME}$"):
        return False

    try:
        _, iterations_text, salt, expected_hex = stored_value.split("$", 3)
        iterations = int(iterations_text)
    except (TypeError, ValueError):
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return secrets.compare_digest(digest.hex(), expected_hex)


def password_needs_rehash(stored_value: str) -> bool:
    if not stored_value.startswith(f"{PASSWORD_SCHEME}$"):
        return True

    try:
        _, iterations_text, _, _ = stored_value.split("$", 3)
        return int(iterations_text) < PBKDF2_ITERATIONS
    except (TypeError, ValueError):
        return True


def encode_signed_payload(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(get_secret_key().encode("utf-8"), body, hashlib.sha256).digest()
    return f"{_b64encode(body)}.{_b64encode(signature)}"


def decode_signed_payload(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None

    try:
        body_b64, signature_b64 = token.split(".", 1)
        body = _b64decode(body_b64)
        signature = _b64decode(signature_b64)
    except Exception:
        return None

    expected_signature = hmac.new(get_secret_key().encode("utf-8"), body, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected_signature):
        return None

    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None
