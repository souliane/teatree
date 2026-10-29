"""Delimit text people wrote as data in a brief, with a closing line the text itself cannot forge (#5685)."""

import hashlib


def fenced(label: str, text: str) -> str:
    """*text* between an opening and a closing line that carry a token derived from the text.

    The token is the text's own digest, so text cannot contain a closing line that matches
    without containing its own digest, and the same text always yields the same brief.
    """
    token = hashlib.sha256(f"{label}\0{text}".encode()).hexdigest()[:16]
    return f"<<<{label} {token}>>>\n{text}\n<<<END {label} {token}>>>"
