# -*- coding: utf-8 -*-
import threading
import time
from typing import Optional

from security import decode_signed_payload, encode_signed_payload

SESSION_TTL_SECONDS = 60 * 60 * 12  # 12 hours

# In-memory session store for server-side revocation and cleanup
# Maps session_id -> {"username": ..., "role": ..., "expires_at": ...}
_session_store: dict[str, dict] = {}
_store_lock = threading.Lock()


def _now() -> int:
    return int(time.time())


def create_session(username: str, role: str) -> str:
    expires_at = _now() + SESSION_TTL_SECONDS
    payload = {
        "username": username,
        "role": role,
        "expires_at": expires_at,
    }
    session_id = encode_signed_payload(payload)
    with _store_lock:
        _session_store[session_id] = payload.copy()
    return session_id


def get_session(session_id: Optional[str]) -> Optional[dict]:
    if not session_id:
        return None
    session = decode_signed_payload(session_id)
    if not session:
        return None
    if int(session.get("expires_at", 0) or 0) <= _now():
        delete_session(session_id)
        return None
    with _store_lock:
        stored = _session_store.get(session_id)
        if stored and int(stored.get("expires_at", 0) or 0) <= _now():
            _session_store.pop(session_id, None)
            return None
    return session


def delete_session(session_id: Optional[str]) -> None:
    if not session_id:
        return
    with _store_lock:
        _session_store.pop(session_id, None)


def clear_expired_sessions() -> None:
    """Remove all expired sessions from the store."""
    now = _now()
    with _store_lock:
        expired = [sid for sid, sess in _session_store.items()
                  if int(sess.get("expires_at", 0) or 0) <= now]
        for sid in expired:
            _session_store.pop(sid, None)
