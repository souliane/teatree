"""Pure normalization and dispatch-fault classification for deferred questions."""

import hashlib
import re

_WHITESPACE_RE = re.compile(r"\s+")


def question_fingerprint(text: str) -> str:
    """Collapse cosmetically different question text to one deduplication marker."""
    normalized = _WHITESPACE_RE.sub(" ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


# Signals that a needs_user_input reason reports a mis-provisioned dispatch,
# not a real owner decision. Each branch keys on a capability failure.
_TOOL_LACK_SELFREPORT_RE = re.compile(
    r"(?:"
    r"\b(?:lack|lacks|lacking|no|without|missing|denied|deprived of)\b[^.]{0,40}?"
    r"\b(?:shell|bash|gh|tool|tools|toolset)\b"
    r"|\bshell[- ]?denied\b"
    r"|\bneeds?\b[^.]{0,40}?\bsession\b[^.]{0,40}?\btool"
    r"|\bsession with (?:the )?(?:standard )?tool"
    r"|\bpicked up by (?:a )?session\b"
    r"|\btool access\b"
    r"|\bno\b[^.]{0,30}?\b(?:accessible )?(?:checkout|working tree|working copy|repo(?:sitory)? access)\b"
    r"|\btask(?:get|list|read)\b[^.]{0,60}?\b(?:returned nothing|nothing|empty|unavailable|no rows)\b"
    r"|\b(?:cannot|can't|can not|unable to|couldn't|could not)\b[^.]{0,60}?"
    r"\b(?:inspect|make code changes|run the required|run [^.]{0,20}?verify-gates|verify-gates"
    r"|clone|check ?out|apply the patch)\b"
    r")",
    re.IGNORECASE,
)


def is_tool_lack_selfreport(text: str) -> bool:
    """Classify an agent's tool-lack self-report as an internal dispatch fault."""
    return bool(_TOOL_LACK_SELFREPORT_RE.search(_WHITESPACE_RE.sub(" ", text.strip())))
