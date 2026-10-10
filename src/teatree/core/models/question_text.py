"""Pure normalization for deferred questions."""

import hashlib
import json
import re
from collections.abc import Sequence

_WHITESPACE_RE = re.compile(r"\s+")


def question_fingerprint(text: str) -> str:
    """Collapse cosmetically different question text to one deduplication marker."""
    normalized = _WHITESPACE_RE.sub(" ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def options_digest(options: Sequence[object]) -> str:
    """The identity of an option set — what a digit reply is checked against before it maps to a label."""
    return hashlib.sha256(json.dumps(options, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
