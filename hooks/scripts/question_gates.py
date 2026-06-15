"""``AskUserQuestion`` decision-policy helpers for the hook router.

A bare sibling module (like ``unknown_repo_push_gate`` / ``mr_cli_fields``):
``hook_router`` is a god-module at its public-function / LOC cap, so the
AskUserQuestion-decision logic lives here and is imported back into the router's
chains rather than growing the router. Two cohesive concerns.

``is_user_directed_question`` is the #807 detection heuristic: does the final
assistant prose pose a decision question directed at the user? Used by the Stop
gate ``handle_enforce_structured_question`` (which keeps the routing decision —
loop-ownership, transcript parsing, the block emit).

``handle_warn_batched_questions`` is the one-decision-per-call advisory: warn
(never block) when an ``AskUserQuestion`` call batches more than one question.
The prose rule (``skills/rules/SKILL.md`` "One decision per question") asks every
decision through its OWN call, never a multi-item batch (unevaluable; one
confusing Slack screen). The #807 Stop gate forces a question through the TOOL
but not one-at-a-time, so this closes that gap. WARN, not block (the user's
choice): a multi-question call still proceeds — stderr (the router's documented
warn channel) carries the nudge so the NEXT decision is split. Fires on EVERY
session. The AI eval ``asks_decisions_one_at_a_time`` pins the behaviour.
"""

import re
import sys

# A '?' is necessary but not sufficient: a second-person/decision cue must also
# be present, which keeps rhetorical asides and explanatory sentences out of the
# #807 gate.
_USER_DIRECTED_CUE_RE = re.compile(
    r"\b("
    r"want me to|should i|shall i|do you want|do you|would you like|"
    r"which (?:one|approach|option|of)|"
    r"prefer|proceed\?|go ahead\?"
    r")\b|\bor\b[^.?!\n]*\?",
    re.IGNORECASE,
)

# A "soft ask" — a deferral phrasing that solicits a user decision WITHOUT a
# literal '?'. "Let me know if/whether …" reads as a status footnote in a loop
# run yet is exactly the lost-decision failure mode #807 targets, so it trips
# the gate independently of the '?' requirement.
_SOFT_ASK_CUE_RE = re.compile(r"\blet me know (?:if|whether|which|what)\b", re.IGNORECASE)

# Fenced code is stripped before the '?'/cue scan so a '?' inside a regex or
# shell glob is not mistaken for a prompt. PUBLIC: the router's
# classifier-relax check (`_is_classifier_relax_explanation`) reuses it.
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)


def is_user_directed_question(text: str) -> bool:
    """True when ``text`` poses a decision question directed at the user.

    Fenced code blocks are stripped first so a ``?`` inside a regex or shell glob
    is not mistaken for a prompt. A "soft ask" ("let me know if/whether …") trips
    the gate on its own — it solicits a decision without a ``?``. Otherwise a
    ``?`` is necessary but not sufficient: a second-person/decision cue must also
    be present, which keeps rhetorical asides and explanatory sentences out.
    """
    prose = FENCED_CODE_RE.sub(" ", text)
    if _SOFT_ASK_CUE_RE.search(prose):
        return True
    if "?" not in prose:
        return False
    return bool(_USER_DIRECTED_CUE_RE.search(prose))


_BATCHED_QUESTION_WARN = (
    "[teatree] AskUserQuestion carried {n} questions in one call. Ask ONE decision "
    "per call (skills/rules/SKILL.md 'One decision per question') — a batched ask "
    "is unevaluable and reads as one confusing Slack screen. Split the rest; ask "
    "the next after this answer."
)


def handle_warn_batched_questions(data: dict) -> None:
    """PreToolUse: warn (never block) when an ``AskUserQuestion`` batches >1 question.

    Advisory only — emits a one-line stderr nudge and returns ``None`` so the
    call always proceeds (warn-don't-block). Silent for a single-question call,
    a non-question tool, or a malformed payload (crash-proof: no ``questions``
    key is treated as zero, never an error).
    """
    if data.get("tool_name") != "AskUserQuestion":
        return
    questions = data.get("tool_input", {}).get("questions")
    if not isinstance(questions, list) or len(questions) <= 1:
        return
    sys.stderr.write(_BATCHED_QUESTION_WARN.format(n=len(questions)) + "\n")
