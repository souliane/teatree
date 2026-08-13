"""The ONE ratification classifier: undecidable never resolves toward the terminal state.

The rejected state both human-gated loops write is terminal with no recovery
transition, so the classifier is asymmetric on purpose — consent and refusal must each
be stated plainly, and everything else defers to a re-ask (souliane/teatree#4184).
"""

import pytest

from teatree.core.models.ratification import RatificationVerdict, classify_ratification_answer

#: Consent, stated up front. The prose shapes are the owner's real recorded wording.
APPROVALS = (
    "approve",
    "RATIFIED, NO SETTING (directive #41). Do NOT mint the setting.",
    "NO SETTING, RATIFIED — just do it.",
    "Approved. The owner approves ALL directives on this box.",
    "lgtm",
)

#: Refusal, stated plainly — a bare denial standing alone, or an unnegated denial verb.
DENIALS = (
    "no",
    "nope",
    "rejected",
    "denied",
    "no, this is the wrong mechanism",
    "Rejected — it duplicates the existing gate.",
    "I refuse to approve this mechanism",
)

#: A negated approval reads as "not yet, here is what I need first" exactly as readily as
#: it reads as a decided refusal, and nothing in the token stream separates the two.
NEGATED_APPROVALS = (
    "not approved yet",
    "cannot approve until the chokepoint is named",
    "I won't approve this without a regression test",
    "I do not approve this.",
    "no approval from me",
    "never going to approve that",
)

#: Neither verdict is stated: prose, a question, a bare denial the sentence takes back.
UNDECIDABLE = (
    "let's talk about this at standup tomorrow",
    "what is the approval policy for this class?",
    "Nope, actually approve it",
    "",
)


@pytest.mark.parametrize("answer", APPROVALS)
def test_consent_stated_up_front_is_an_approval(answer: str) -> None:
    assert classify_ratification_answer(answer) is RatificationVerdict.APPROVAL


@pytest.mark.parametrize("answer", DENIALS)
def test_a_plainly_stated_refusal_stays_expressible(answer: str) -> None:
    assert classify_ratification_answer(answer) is RatificationVerdict.DENIAL


@pytest.mark.parametrize("answer", [*NEGATED_APPROVALS, *UNDECIDABLE])
def test_an_undecidable_answer_never_reads_as_a_refusal(answer: str) -> None:
    assert classify_ratification_answer(answer) is RatificationVerdict.UNRECOGNISED


def test_a_verdict_buried_below_the_lead_window_decides_nothing() -> None:
    assert classify_ratification_answer("so anyway I guess approve") is RatificationVerdict.UNRECOGNISED


def test_only_the_first_line_opening_sentence_decides() -> None:
    # An amendment body legitimately says "do NOT mint" — that must not overrule the
    # verdict the first line already gave.
    answer = "RATIFIED.\nDo NOT mint the setting; make it unconditional."
    assert classify_ratification_answer(answer) is RatificationVerdict.APPROVAL
