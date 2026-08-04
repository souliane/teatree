r"""Answer-first detector — the inverse of the structured-question gate.

The structured-question gate (#807) fires when the AGENT poses a question in
prose without the structured tool. Its inverse had no counterpart: the USER asks
something answerable and the agent replies with a delegation report — "dispatched
a lane to merge it" — which reports orchestration and answers nothing, so the
owner asks the same thing a second time. Acting is not answering.

This is the pure detector the Stop handler ``handle_answer_first_gate``
(``hooks/scripts/answer_first_gate.py``) uses. It is a three-way conjunction, all
three legs required, so precision comes from the shape rather than from any one
list of words:

1. the last user message asks something ANSWER-SEEKING — an explanation demand,
    or an interrogative carrying a second-person / state cue;
2. the final assistant turn reports a DELEGATION — the work was handed to a lane,
    a sub-agent, a task;
3. that turn carries NO answer — no polarity opener, no causal explanation, and
    no honest "I do not know yet".

A turn that answers and then dispatches clears leg 3 on one word, which is the
whole behaviour being asked for. A dispatch report following a non-question
clears leg 1. An answer with no delegation clears leg 2. Fail-safe-to-silent:
empty or odd input yields ``None``.
"""

import re
from typing import Final, NamedTuple

_FENCED_CODE_RE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)

# One interrogative run, bounded to its own sentence so a '?' three sentences
# away from the cue is not read as one question.
_QUESTION_SPAN_RE: Final[re.Pattern[str]] = re.compile(r"[^.!?\n]{0,300}\?")

# A cue that the '?' is directed at the agent and wants an ANSWER: an explanation
# word, or a second-person / state interrogative. A bare '?' is deliberately not
# enough — an id in parentheses ("ship it (4001?)") must never fire.
#
# A bare modal second person is NOT a cue. "can you merge this?", "could you
# rebase it?", "will you push that?" are POLITE IMPERATIVES: the '?' is courtesy
# and the ask is for the WORK, which a dispatch report answers perfectly well.
# They only seek an answer when an information verb follows ("can you confirm the
# pipeline is green?"), so the modal alternative requires one. "do you" is
# narrowed the same way, to the forms asking about state, knowledge or
# preference ("do you want me to merge it?"). A modal question that really is
# polar still fires through its own tail ("will you merge it or not?").
_ANSWER_SEEKING_CUE_RE: Final[re.Pattern[str]] = re.compile(
    r"\bwhy\b|\bhow come\b|\bon what basis\b|"
    r"\bwhat (?:were|was|is|are|caused|went wrong|broke|blocked|happened)\b|"
    r"\bwhat'?s the (?:reason|cause|problem|blocker|status)\b|"
    r"\bare you\b|\bdid you\b|\bhave you\b|"
    r"\bdo you (?:want|think|know|mean|agree|recall|remember)\b|"
    r"\b(?:can|could|will|would) you\s+(?:tell me|explain|clarify|confirm|say|know|describe|"
    r"elaborate|remind me|let me know|walk me through|talk me through)\b|"
    r"\bare we\b|\bdid we\b|\bis it\b|\bwas it\b|\bdoes it\b|\bdid it\b|\bis that\b|"
    r"\bor not\b|\byes or no\b",
    re.IGNORECASE,
)

# An explanation demanded without a '?' — an imperative that still wants prose
# back, not work ("tell me why it was not mergeable").
_EXPLANATION_DEMAND_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:tell me|explain|walk me through|talk me through)\b[^.\n]{0,60}\bwhy\b|"
    r"\bexplain (?:the|what) (?:reason|cause|failure|problem|went wrong)\b",
    re.IGNORECASE,
)

# The reply shape that reports orchestration instead of answering: the work went
# to someone else. Narrow on purpose — "merging it now" is a first-person action
# that at least tells the asker what happens next, and never fires this leg.
_DELEGATION_REPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"\bre-?dispatch(?:ed|ing)\b|\bdispatch(?:ed|ing)\b|\bdelegated\b|\bfanned out\b|"
    r"\b(?:spawned|launched|kicked off|started|queued|fired off) (?:a|an|the|another)\s+"
    r"(?:lane|agent|sub-?agent|worker|task|job|run)\b|"
    r"\bhanded (?:it|this|that|the \w+)?\s*(?:off\s+)?to\b|"
    r"\bassigned (?:it|this|that) to\b|"
    r"\b(?:a|the|another) (?:lane|sub-?agent|agent|worker) (?:is|was) (?:now )?"
    r"(?:running|working|on it|dispatched|picking)\b",
    re.IGNORECASE,
)

# A polarity opener standing at the head of a line — the shortest possible answer
# to a yes/no question, and the one the recorded failure omitted.
_POLARITY_OPENER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t>*_+-]*(?:\*\*|__)?(?:yes|no|nope|yep|not yet|neither|both|correct|incorrect)\b",
    re.IGNORECASE | re.MULTILINE,
)

# An explanation, or an honest admission that there is not one yet. Either
# discharges the question; only silence does not.
_EXPLANATION_GIVEN_RE: Final[re.Pattern[str]] = re.compile(
    r"\bbecause\b|\bthe reason\b|\broot cause\b|\bcaused by\b|\bdue to\b|\bthe cause (?:was|is)\b|"
    r"\bit failed on\b|\bthe blocker (?:was|is)\b|\bwhat went wrong (?:was|is)\b|\bthe answer is\b|"
    r"\b(?:i|we) (?:don'?t|do not) know\b|\bno answer yet\b|\bcan'?t answer\b|\bcannot answer\b|"
    r"\b(?:haven'?t|have not|hasn'?t|has not) (?:yet )?(?:read|opened|checked|looked|seen|inspected)\b|"
    r"\bunverified\b|\bunconfirmed\b",
    re.IGNORECASE,
)


class AnswerVerdict(NamedTuple):
    """The question that went unanswered and the delegation that stood in for it."""

    question: str
    action: str


def _answer_seeking_question(user_text: str) -> str | None:
    prose = _FENCED_CODE_RE.sub(" ", user_text)
    demand = _EXPLANATION_DEMAND_RE.search(prose)
    if demand is not None:
        return demand.group(0).strip()
    for span in _QUESTION_SPAN_RE.finditer(prose):
        question = span.group(0).strip()
        if _ANSWER_SEEKING_CUE_RE.search(question):
            return question
    return None


def _delegation_report(agent_text: str) -> str | None:
    match = _DELEGATION_REPORT_RE.search(agent_text)
    return match.group(0).strip() if match is not None else None


def _carries_an_answer(agent_text: str) -> bool:
    return bool(_POLARITY_OPENER_RE.search(agent_text) or _EXPLANATION_GIVEN_RE.search(agent_text))


def find_unanswered_question(user_text: str, agent_text: str) -> AnswerVerdict | None:
    """Return a verdict to BLOCK, or ``None`` to allow.

    Fires only on the full conjunction: an answer-seeking question in
    *user_text*, a delegation report in *agent_text*, and no answer anywhere in
    *agent_text*. Any leg missing yields ``None``.
    """
    if not user_text or not agent_text:
        return None
    question = _answer_seeking_question(user_text)
    if question is None:
        return None
    action = _delegation_report(agent_text)
    if action is None or _carries_an_answer(agent_text):
        return None
    return AnswerVerdict(question=question, action=action)


def format_block_message(verdict: AnswerVerdict) -> str:
    """Render the BLOCK reason naming the question and the delegation that displaced it."""
    return (
        "ANSWER-FIRST GATE — the user asked a question and this turn reports a "
        f'delegation instead of answering it.\n  asked: "{verdict.question}"\n'
        f'  replied: "{verdict.action}"\n'
        "An interrogative is not an instruction: dispatching work says nothing the "
        "asker wanted to know, and they have to ask again. Answer it in this turn — "
        "the polarity first on a yes/no question ('Yes, merging it now'), the cause on "
        "a why question ('it was blocked by <X>, here is the line'). If you do not "
        "know yet, say so plainly ('I have not read the logs yet') — that is an answer. "
        "Then keep the dispatch. Escape for a genuine false fire: end the turn with "
        "[skip-answer-gate: <reason>]."
    )
