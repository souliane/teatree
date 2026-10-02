"""The measured false-fire corpus for the answer-first detector.

Every case here is a turn the gate BLOCKED in a recorded measurement while the
owner was mid-conversation. The gate does not skip an attended turn, so each of
these landed on the owner as "you did not answer me" against a turn that plainly
did. Two independent defects produced them and both passed all 35 of the gate's
own tests, because every one of those tests puts the answer in the FINAL text
block — a transcript shape production never produces.

Two halves, and the second is what makes the first mean anything:

*   ten ALLOW rows — four answers written before the dispatch tool call, six
    answers whose causal wording was simply not on the allowlist;
*   four BLOCK rows — the bare dispatch report the gate exists for. These are the
    CONTROL. A widening that turns them green has deleted the gate rather than
    fixed it, and the corpus would then pass while asserting nothing.
"""

import pytest

from teatree.hooks.answer_first_scanner import find_unanswered_question

_WHY_NOT_MERGED = "why was 4193 not merged?"
_IS_IT_GREEN = "did you get the shard green or not?"

# The §B.1 half: the answer sits in the assistant block written BEFORE the
# dispatch tool call, so `last_assistant_turn` used to cut the turn at the
# tool_result boundary and never see it. The scanner is handed the WHOLE turn
# text here — the parser fix is what makes that text arrive intact.
_ANSWER_BEFORE_THE_DISPATCH = [
    ("explicit because", _WHY_NOT_MERGED, "Because the eval lane is red.\nDispatched a lane to fix it."),
    ("polarity opener", _IS_IT_GREEN, "No — the shard is red.\nDispatched a lane to fix it."),
    ("honest not-yet", _WHY_NOT_MERGED, "I don't know yet.\nDispatched a lane to find out."),
    (
        "answer then delegation report",
        _WHY_NOT_MERGED,
        "The merge queue rejected it on a failing required check.\nHanded it off to a reviewer agent.",
    ),
]

# The §B.3 half: natural causal answers whose wording is not on the allowlist.
# None of these carries "because" / "the reason" / "root cause"; all of them
# answer the question. This is why leg 3 cannot be an allowlist.
_NATURAL_CAUSAL_ANSWERS = [
    ("blocked-by phrasing", _WHY_NOT_MERGED, "It was blocked by a failing shard.\nDispatched a lane to fix it."),
    ("bare noun-phrase cause", _WHY_NOT_MERGED, "Conflicts with main.\nDispatched a lane to resolve them."),
    ("still-waiting-on", _WHY_NOT_MERGED, "It is still waiting on CI.\nDispatched a lane to watch it."),
    (
        "state description",
        _WHY_NOT_MERGED,
        "The branch is behind main by eleven commits.\nDispatched a lane to update it.",
    ),
    ("policy cause", _WHY_NOT_MERGED, "The merge keystone needs a human approver.\nDispatched a lane to request one."),
    ("review-state cause", _WHY_NOT_MERGED, "A reviewer left it on hold last night.\nDispatched a lane to address it."),
]

# The control. Nothing but the dispatch line and its boilerplate — the recorded
# failure this gate was built for, verbatim.
_BARE_DISPATCH_REPORTS = [
    ("the recorded failure", _WHY_NOT_MERGED, "Dispatched a lane to merge it."),
    ("dispatch plus a promise to return", _WHY_NOT_MERGED, "I've dispatched a sub-agent. Will report back."),
    ("handoff plus a status echo", _WHY_NOT_MERGED, "Handed it off to a reviewer agent. Standing by."),
    ("fan-out plus an acknowledgement", _IS_IT_GREEN, "On it. Fanned out three lanes."),
]


@pytest.mark.parametrize(
    ("label", "user_text", "agent_text"),
    _ANSWER_BEFORE_THE_DISPATCH + _NATURAL_CAUSAL_ANSWERS,
    ids=lambda value: value if isinstance(value, str) and " " in value else None,
)
def test_a_measured_false_fire_is_allowed(label: str, user_text: str, agent_text: str) -> None:
    assert find_unanswered_question(user_text, agent_text) is None, label


@pytest.mark.parametrize(
    ("label", "user_text", "agent_text"),
    _BARE_DISPATCH_REPORTS,
    ids=lambda value: value if isinstance(value, str) and " " in value else None,
)
def test_a_bare_dispatch_report_still_blocks(label: str, user_text: str, agent_text: str) -> None:
    verdict = find_unanswered_question(user_text, agent_text)
    assert verdict is not None, label


def test_the_corpus_covers_every_measured_false_fire() -> None:
    # The corpus is evidence for an ENABLED gate, so its size is part of the
    # claim: ten measured false fires, four true positives holding them honest.
    assert len(_ANSWER_BEFORE_THE_DISPATCH) == 4
    assert len(_NATURAL_CAUSAL_ANSWERS) == 6
    assert len(_BARE_DISPATCH_REPORTS) >= 3
