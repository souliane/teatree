"""Stable privacy-preserving join key for an agent session."""

import hashlib


def session_ref(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16] if session_id else ""
