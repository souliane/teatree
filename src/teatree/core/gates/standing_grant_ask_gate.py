"""Refuse an ``AskUserQuestion`` asking the owner to sign off a substrate merge a standing grant holds.

An owner who granted standing autonomy was still asked, per PR, to approve
substrate merges their standing grant (``substrate_self_signoff`` at
``autonomy = full``, or the ``substrate_auto_merge_authorized_by`` delegation)
already authorizes (#2663). This is the pure, Django-free phrasing classifier;
the PreToolUse hook (``hooks/scripts/standing_grant_ask_gate.py``) reads the
grant and emits the deny only when one is configured.

An ask matches when it seeks sign-off AND names a substrate merge, and carries
no cue that it is really about something the grant does not cover — a floor
waiver, an override of a HOLD, a close/revert, or changing the grant itself.
"""

import re
from dataclasses import dataclass

_SIGN_OFF_RE = re.compile(
    r"\b(?:approv\w*|authori[sz]\w*|sign[- ]?off|confirm\w*|permission|green[- ]?light\w*|go[- ]ahead|ok(?:ay)? to)\b"
    r"|\b(?:shall|should|can|may) I (?:go ahead and |now )?(?:merge|clear|land)\b"
    r"|\bwant me to (?:go ahead and )?(?:merge|clear|land)\b"
    r"|\bproceed with (?:the )?(?:merg|clear|land)\w*",
    re.IGNORECASE,
)

_MERGE_WORD = r"(?:merg\w*|clear(?:s|ed|ing)?|land(?:s|ed|ing)?)"
# At most 80 characters, never across a sentence end — a URL's dots do not end a sentence.
_SAME_SENTENCE = r"(?:(?![.?!](?:\s|$))[^\n]){0,80}?"
_SUBSTRATE_MERGE_RE = re.compile(
    rf"--human-authori[sz]ed?\b"
    rf"|\bsubstrate\b{_SAME_SENTENCE}\b{_MERGE_WORD}\b"
    rf"|\b{_MERGE_WORD}\b{_SAME_SENTENCE}\bsubstrate\b",
    re.IGNORECASE,
)

_NOT_COVERED_RE = re.compile(
    r"\b(?:expedit\w*|waiv\w*|pending checks?|checks? (?:are |still )*pending|force[- ]?push\w*"
    r"|history rewrite|rewrit\w* (?:the )?history|overrid\w*|bypass\w*|despite"
    r"|revok\w*|revert\w*|disable[sd]?|disabling|turn(?:ing)? off|unset\w*|close|closing|rebas\w*|conflict\w*)\b",
    re.IGNORECASE,
)

_OK_TOKEN_RE = re.compile(r"\[grant-ask-ok:\s*(\S[^\]]*?)\s*\]")
_TOKEN_SCAN_CHARS = 512

_FORGE_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_OWNER_REPO_REF_RE = re.compile(r"(?<![\w/.-])([\w.-]+/[\w.-]+)[#!]\d+\b")

_QUOTED_QUESTION_CHARS = 160


@dataclass(frozen=True, slots=True)
class StandingGrantAsk:
    """One question that asks the owner to sign off a grant-covered substrate merge."""

    question: str


def _asks_for_a_covered_sign_off(text: str) -> bool:
    return bool(_SIGN_OFF_RE.search(text) and _SUBSTRATE_MERGE_RE.search(text) and not _NOT_COVERED_RE.search(text))


def find_standing_grant_ask(questions: list[str]) -> StandingGrantAsk | None:
    """The first question asking for a substrate-merge sign-off a standing grant would cover, else None."""
    for question in questions:
        if question and _asks_for_a_covered_sign_off(question):
            return StandingGrantAsk(question=question)
    return None


def repo_refs(text: str) -> tuple[str, ...]:
    """Forge URLs and ``owner/repo`` slugs (from ``owner/repo#N`` / ``!N``) named in *text*, in order."""
    found = [(m.start(), m.group(0)) for m in _FORGE_URL_RE.finditer(text)]
    found += [(m.start(), m.group(1)) for m in _OWNER_REPO_REF_RE.finditer(text)]
    return tuple(ref for _, ref in sorted(found))


def grant_ask_ok_reason(text: str) -> str | None:
    """The reason from a ``[grant-ask-ok: <reason>]`` escape token in *text*, else None."""
    if not text:
        return None
    match = _OK_TOKEN_RE.search(text[:_TOKEN_SCAN_CHARS])
    return match.group(1).strip() or None if match else None


def deny_reason(finding: StandingGrantAsk, *, overlay: str, delegated_by: str) -> str:
    """The refusal: which grant already holds the sign-off, and how to merge without asking."""
    if delegated_by:
        grant = f"`substrate_auto_merge_authorized_by = {delegated_by}`"
        clear = f"`t3 <overlay> ticket clear … --blast-class substrate --human-authorize {delegated_by}`"
    else:
        grant = "`substrate_self_signoff` at `autonomy = full`"
        clear = "`t3 <overlay> ticket clear …` (no per-PR authorizer needed)"
    question = finding.question[:_QUOTED_QUESTION_CHARS]
    return (
        f"BLOCKED: this question asks the owner to sign off a substrate merge — «{question}» — but the "
        f"standing grant on overlay `{overlay}` ({grant}) already authorizes it, and the owner asked not "
        "to be asked per PR. Decide it yourself against the bar: an independent cold review MERGE_SAFE on "
        f"the live head and CI green, then {clear} and `t3 <overlay> ticket merge` — the keystone re-checks "
        "the grant and every floor gate. If the question is about something the grant does not cover, add "
        "a `[grant-ask-ok: <reason>]` token to it, or disable the gate with "
        "`t3 <overlay> gate standing-grant-ask disable`."
    )


__all__ = [
    "StandingGrantAsk",
    "deny_reason",
    "find_standing_grant_ask",
    "grant_ask_ok_reason",
    "repo_refs",
]
