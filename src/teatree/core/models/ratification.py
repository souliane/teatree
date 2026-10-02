"""The ONE ratification classifier — every reader of a human's ratify answer.

Both human-gated loops ask the same question ("approve to admit?"), both write a TERMINAL
rejected state from the answer, and the approval dial scores the same answers to decide
whether to re-tighten a graduated class. That is one reading, not three: a copy drifts
silently, which is exactly how the outer loop kept an eight-token exact match long after
the directive loop learned to read prose (souliane/teatree#4184, souliane/teatree#4187).
A leaf module (no DB, no gate imports) so ``core`` and the loops can both reach it —
``core`` cannot import a loop, which is why this does not live beside its callers.

The safety property, stated once: **an answer the classifier cannot confidently read as
a refusal must never reach a terminal state.** A re-ask costs one round trip; a terminal
reject destroys an owner decision with no recovery transition. So ``DENIAL`` is reserved
for a bare refusal standing alone, or an unnegated denial verb stated up front —
everything undecidable, including a NEGATED approval, defers.
"""

import re
from enum import Enum


class RatificationVerdict(Enum):
    """What a human's recorded ratify answer decides — three-valued, never two.

    ``UNRECOGNISED`` is the load-bearing member: an answer the classifier cannot read
    as consent OR as refusal decides nothing, and the rejected state is terminal with no
    recovery transition, so undecidable must never collapse into denial.
    """

    APPROVAL = "approval"
    DENIAL = "denial"
    UNRECOGNISED = "unrecognised"


_APPROVAL_LEMMAS = frozenset(
    {
        "approve",
        "approved",
        "approves",
        "approval",
        "ratify",
        "ratified",
        "ratifies",
        "admit",
        "admitted",
        "accept",
        "accepted",
        "agreed",
        "yes",
        "yep",
        "yeah",
        "y",
        "ok",
        "okay",
        "lgtm",
        "1",
    }
)

#: Denial VERBS state a refusal wherever they lead the answer.
_DENIAL_VERBS = frozenset(
    {
        "reject",
        "rejected",
        "rejects",
        "deny",
        "denied",
        "denies",
        "decline",
        "declined",
        "declines",
        "disapprove",
        "disapproved",
        "refuse",
        "refused",
        "veto",
        "vetoed",
    }
)

#: Bare refusal markers. ``no`` is also an ordinary determiner ("RATIFIED, NO SETTING"),
#: so these count only when they stand alone as the answer's opening clause.
_BARE_DENIALS = frozenset({"no", "n", "nope", "nah", "0"})


#: ``no`` negates only what it directly modifies ("no approval from me"), never a lemma
#: a clause away — "NO SETTING, RATIFIED" is an approval, not a refusal.
_ADJACENT_NEGATORS = frozenset({"no"})

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_APOSTROPHE_RE = re.compile(r"['\u2019]")
_SENTENCE_END_RE = re.compile(r"[.!?]")
_CLAUSE_END_RE = re.compile(r"[,;:]")

#: Spelled with their apostrophes and stripped the way :func:`_tokens` strips them, so
#: the set matches the tokens the tokeniser actually emits.
_NEGATORS = frozenset(
    _APOSTROPHE_RE.sub("", word)
    for word in ("not", "never", "cannot", "can't", "won't", "don't", "doesn't", "didn't", "refuse", "without")
)

#: A verdict is stated up front, so an approval lemma buried deeper than this in the
#: opening sentence is prose, not consent — it defers rather than admits.
_LEAD_WINDOW = 2

#: How far back a negator reaches when deciding whether a lemma was negated.
_NEGATION_WINDOW = 3


def classify_ratification_answer(answer: str) -> RatificationVerdict:
    """Read a human's prose ratification as consent, refusal, or neither.

    Conservative in BOTH directions, and asymmetrically so toward the terminal side.
    Consent must be stated up front and unnegated. Refusal needs a bare "no" standing
    alone as the opening clause with nothing in the sentence taking it back, or an
    unnegated denial verb stated up front. Everything else — a passing mention of
    "approval" deeper in a sentence, a negated approval ("not approved yet", "I do not
    approve this"), a bare denial the same sentence contradicts ("Nope, actually approve
    it") — is ``UNRECOGNISED`` and re-asked.

    A negated approval reads as *not yet, here is what I need first* as readily as it
    reads as a decided refusal, and nothing in the token stream separates the two. The
    owner who meant to refuse still has "no", "reject" and "denied".
    """
    head = _decision_head(answer)
    tokens = _tokens(head)
    opening = _tokens(_CLAUSE_END_RE.split(head, maxsplit=1)[0])
    if len(opening) == 1 and opening[0] in _BARE_DENIALS:
        return _bare_denial_verdict(tokens)
    return _lead_verdict(tokens)


def _bare_denial_verdict(tokens: list[str]) -> RatificationVerdict:
    """A bare "no" refuses only while nothing in its own sentence takes it back."""
    if any(token in _APPROVAL_LEMMAS for token in tokens):
        return RatificationVerdict.UNRECOGNISED
    return RatificationVerdict.DENIAL


def _lead_verdict(tokens: list[str]) -> RatificationVerdict:
    """The verdict the first approval-or-denial lemma states, or none at all."""
    for index, token in enumerate(tokens):
        if token in _APPROVAL_LEMMAS:
            return _as_stated(RatificationVerdict.APPROVAL, tokens, index)
        if token in _DENIAL_VERBS:
            return _as_stated(RatificationVerdict.DENIAL, tokens, index)
    return RatificationVerdict.UNRECOGNISED


def _as_stated(verdict: RatificationVerdict, tokens: list[str], index: int) -> RatificationVerdict:
    """*verdict*, unless its lemma was negated or buried below the lead window."""
    if _is_negated(tokens, index) or index > _LEAD_WINDOW:
        return RatificationVerdict.UNRECOGNISED
    return verdict


def _decision_head(answer: str) -> str:
    """The opening sentence of the first non-empty line — where a verdict is stated.

    A ratification body goes on to spell out amendments in prose that legitimately
    contains "do NOT mint …" and "no setting"; reading the whole body would let that
    detail overrule the verdict its own first line already gave.
    """
    lines = [line for line in answer.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    end = _SENTENCE_END_RE.search(lines[0])
    return lines[0][: end.start()] if end else lines[0]


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(_APOSTROPHE_RE.sub("", text.lower()))


def _is_negated(tokens: list[str], index: int) -> bool:
    if any(token in _NEGATORS for token in tokens[max(0, index - _NEGATION_WINDOW) : index]):
        return True
    return index > 0 and tokens[index - 1] in _ADJACENT_NEGATORS
