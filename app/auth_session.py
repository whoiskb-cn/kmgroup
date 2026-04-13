# -*- coding: utf-8 -*-
import time
from typing import Optional

from app.security import decode_signed_payload, encode_signed_payload

SESSION_TTL_SECONDS = 60 * 60 * 12  # 12 hours


def _now() -> int:
    return int(time.time())


def create_session(username: str, role: str) -> str:
    expires_at = _now() + SESSION_TTL_SECONDS
    return encode_signed_payload(
        {
            "username": username,
            "role": role,
            "expires_at": expires_at,
        }
    )


def get_session(session_id: Optional[str]) -> Optional[dict]:
    session = decode_signed_payload(session_id)
    if not session:
        return None
    if int(session.get("expires_at", 0) or 0) <= _now():
        return None
    return session


def delete_session(session_id: Optional[str]) -> None:
    return None


def clear_expired_sessions() -> None:
    return None
