"""``AskUserQuestion`` decision-policy helpers for the hook router.

A bare sibling module (like ``unknown_repo_push_gate`` / ``mr_cli_fields``):
``hook_router`` is a god-module at its public-function / LOC cap, so the
AskUserQuestion-decision logic lives here and is imported back into the router's
chains rather than growing the router. Two cohesive concerns.

``is_user_directed_question`` is the #807 detection heuristic: does the final
assistant prose pose a decision question directed at the user? Used by the Stop
gate ``handle_enforce_structured_question`` (which keeps the routing decision —
loop ownership, transcript parsing, the block emit).

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

import json
import re
import sys
from pathlib import Path

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

# An ANNOUNCED ask — the turn narrates an imminent user ask ("**Action:** Ask
# about the first PR", "I'll ask the user which branch") without issuing it.
# There is no '?' and no soft-ask cue, so the two trip-wires above miss it, yet
# on a loop turn the narration reads as a log line and the decision is silently
# lost — the exact metered red `structured_question_one_decision_per_question`
# pinned. Every shape requires an ask-object ("the user"/"about"/"whether"/…)
# AND a first-person / action-line anchor, so a negated resolution ("No need to
# ask the user — I resolved it"), a rule citation ("a blocked sub-agent must ask
# via AskUserQuestion"), a third-party subject ("the reviewer should ask for a
# URL"), a quoted heading ("## Ask About Auth …"), and a past-tense report
# ("the ticket asked for a doc update") never fire.
_ASK_OBJECT = r"(?:about|the user|you|for|whether|which)"
_ANNOUNCED_ASK_RE = re.compile(
    rf"\bi(?:(?:'|\u2019)?ll| will| am going to|(?:'|\u2019)?m going to| am about to| need to| must"
    rf"| should| have to| want to)? ask {_ASK_OBJECT}\b"
    rf"|\b(?:let me|time to) ask {_ASK_OBJECT}\b"
    rf"|\b(?:action|next(?: step)?|now|plan|step \d+)\b\W{{0,4}}ask {_ASK_OBJECT}\b"
    rf"|^(?:[-*>\u2022]\s{{0,2}}|\*\*)?ask {_ASK_OBJECT}\b",
    re.IGNORECASE | re.MULTILINE,
)

# The legitimate one-ask-then-wait disposition suppresses the announced-ask wire
# — forcing a re-ask there would create the exact re-ask loop
# `asks_decisions_one_at_a_time` forbids. Two shapes: an explicit waiting
# posture ("pausing for your answer"), or an answer-gated next step ("once you
# answer, I'll ask the second decision") — the latter only with evidence a
# question was actually put to the user (a past-ask verb), so "I'll ask which
# branch; once you answer, I'll merge" (nothing ever asked) still blocks. (A
# same-turn real ask short-circuits earlier via `used_question_tool`.)
_WAITING_DISPOSITION_RE = re.compile(
    r"\b(?:await(?:ing)?|waiting|paus\w+|hold(?:ing)?)\b[^.!\n]{0,60}\b(?:answer|reply|response|input|decision)\b",
    re.IGNORECASE,
)
_ANSWER_GATED_NEXT_RE = re.compile(
    r"\b(?:once|after|when)\b[^.!\n]{0,40}\b(?:answer|respond|reply|response|input)\b",
    re.IGNORECASE,
)
_PAST_ASK_SIGNAL_RE = re.compile(r"\b(?:asked|surfaced|posed|raised|posted)\b", re.IGNORECASE)


def _pending_answer_disposition(prose: str) -> bool:
    if _WAITING_DISPOSITION_RE.search(prose):
        return True
    return bool(_ANSWER_GATED_NEXT_RE.search(prose) and _PAST_ASK_SIGNAL_RE.search(prose))


# A PRINTED tool call — the model emits `AskUserQuestion(...)` call SYNTAX as
# text (fenced or inline) instead of issuing the tool_use block, believing the
# printed call is the ask. Scanned on the RAW text (before the fence strip —
# a fenced printed call is exactly the give-away), and NEVER suppressed by the
# pending-answer disposition (printed call syntax is wrong in every posture).
# Call SHAPE required — the paren must open a questions payload — so a prose
# MENTION of the tool or a backticked symbol reference ("the `AskUserQuestion(`
# printed-call detector") never fires.
_PRINTED_TOOL_CALL_RE = re.compile(r"AskUserQuestion\s*\(\s*(?:questions|\[|\{|[\"'])")

# The #807 Stop-gate block reason — owned here beside the detection heuristic it
# explains (the shrink-only router imports it back).
STRUCTURED_QUESTION_BLOCK = (
    "TEATREE GATE — a user-directed question was posed inline in prose (or "
    "announced/printed — 'I'll ask…' / a literal AskUserQuestion(...) line — "
    "without being issued) with no AskUserQuestion tool call in this turn. "
    "Inline/narrated questions are invisible in autonomous/loop runs (they read "
    "as log lines) so the decision is lost. Issue a REAL AskUserQuestion tool "
    "call NOW — an actual tool invocation, never call syntax printed as text — "
    "one question at a time, with concrete options — then continue. This is a "
    "non-bypassable gate (no `relax:` escape): the question must go through the "
    "structured tool."
)

# A SEQUENCING offer about the agent's OWN next action — "want me to write X now
# or react first?" — where BOTH branches are the agent's own work and the question
# is only the ORDER (a "now …" against a "… first/next/then" ordering word). The
# agent resolves its own sequencing autonomously on a loop turn, so this rhetorical
# offer is NOT the lost user DECISION the #807 gate targets (the documented false
# positive). Deliberately NARROW: it requires the "want me to" lead AND a "now" AND
# an explicit ordering word AROUND the "or", so a genuine go/no-go ("shall I
# merge?", "do you want me to open the PR?", "push now or wait for review?") and a
# real target choice ("deploy staging now or production?" — no ordering word) all
# still fire.
_SEQUENCING_OFFER_RE = re.compile(
    r"\bwant me to\b[^?]*"
    r"(?:"
    r"\bnow\b[^?]*\bor\b[^?]*\b(?:first|next|then|later|afterwards?|instead)\b"
    r"|\b(?:first|next|then|later|afterwards?|instead)\b[^?]*\bor\b[^?]*\bnow\b"
    r")[^?]*\?",
    re.IGNORECASE,
)


def is_user_directed_question(text: str) -> bool:
    """True when ``text`` poses a decision question directed at the user.

    Fenced code blocks are stripped first so a ``?`` inside a regex or shell glob
    is not mistaken for a prompt. A "soft ask" ("let me know if/whether …") trips
    the gate on its own — it solicits a decision without a ``?``. Otherwise a
    ``?`` is necessary but not sufficient: a second-person/decision cue must also
    be present, which keeps rhetorical asides and explanatory sentences out.

    A narrow SEQUENCING offer about the agent's own next action ("want me to
    write X now or react first?") does NOT fire — it asks only the order of the
    agent's own work, which it resolves autonomously. A genuine go/no-go or a
    real choice between substantive options still fires.

    An ANNOUNCED ask ("**Action:** Ask about the first PR", "I'll ask the user
    which branch") and a PRINTED tool call (`AskUserQuestion(...)` emitted as
    text, fenced or inline) trip independently of the ``?`` requirement too —
    narrating or printing the ask is not asking, and on a loop turn the decision
    is silently lost. The one-ask-then-wait disposition ("once you answer, I'll
    ask the second decision", with evidence something was asked) suppresses only
    the announced-ask wire so a compliant walk-through is never re-ask-looped;
    printed call syntax is wrong in every posture and is never suppressed.
    """
    prose = FENCED_CODE_RE.sub(" ", text)
    if _SOFT_ASK_CUE_RE.search(prose):
        return True
    if _PRINTED_TOOL_CALL_RE.search(text):
        return True
    if _ANNOUNCED_ASK_RE.search(prose) and not _pending_answer_disposition(prose):
        return True
    if "?" not in prose:
        return False
    if not _USER_DIRECTED_CUE_RE.search(prose):
        return False
    return not _SEQUENCING_OFFER_RE.search(prose)


# A user CLARIFICATION request — they did not pick an option, they asked the
# agent to re-explain ("what do you mean", "clarify", "none of these"). When this
# follows an AskUserQuestion the harness already routes the re-ask, so the agent's
# prose clarification must NOT be force-gated into a second AskUserQuestion (#807
# false positive).
_CLARIFY_REQUEST_RE = re.compile(
    r"\bclarif\w*"
    r"|\bwhat do you mean\b"
    r"|\bi (?:don'?t|do not) (?:understand|follow|get)\b"
    r"|\brephrase\b|\belaborate\b"
    r"|\bnot (?:clear|sure what you)\b|\bunclear\b"
    r"|\bnone of (?:these|those|them)\b"
    r"|\bthat'?s not what (?:i|we)\b"
    r"|\bcan you explain\b",
    re.IGNORECASE,
)


def read_transcript_entries(transcript_path: str) -> list[dict]:
    """Parse the Claude Code transcript JSONL into a list of dict entries.

    Fail-safe: an empty/missing/unreadable file or malformed lines yield
    ``[]`` (the caller then does nothing) rather than raising. Owned here (the
    transcript-parsing home) and imported back into ``hook_router`` so the
    god-module stays under its LOC cap.
    """
    if not transcript_path:
        return []
    path = Path(transcript_path)
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[dict] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _entry_message_role(entry: dict) -> str | None:
    message = entry.get("message")
    return message.get("role") if isinstance(message, dict) else entry.get("type")


def _entry_message_blocks(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content", []) if isinstance(message, dict) else []
    return content if isinstance(content, list) else []


def last_assistant_turn(transcript_path: str) -> tuple[str, bool] | None:
    """Return ``(final_assistant_text, used_question_tool)`` for the last turn.

    The "last turn" is every assistant message after the most recent user
    message in the transcript JSONL. ``final_assistant_text`` is the concatenated
    text blocks of those messages; ``used_question_tool`` is ``True`` if any
    ``AskUserQuestion`` ``tool_use`` block appears in the turn. Returns ``None``
    when the transcript is missing, unreadable, empty, or has no trailing
    assistant turn (fail-safe to "do nothing"). Owned here (the transcript-parsing
    home) and imported back into ``hook_router`` to keep that god-module shrinking.
    """
    texts: list[str] = []
    used_tool = False
    for entry in reversed(read_transcript_entries(transcript_path)):
        role = _entry_message_role(entry)
        if role == "user":
            break
        if role != "assistant":
            continue
        for block in _entry_message_blocks(entry):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
                used_tool = True
    if not texts:
        return None
    # entries were walked newest→oldest; restore reading order
    return "\n".join(reversed(texts)), used_tool


def preceding_user_rejected_question_and_asked_clarify(entries: list[dict]) -> bool:
    """True when the user just rejected an AskUserQuestion and asked to clarify.

    Walks the transcript newest→oldest through three phases: the trailing
    assistant turn under inspection (skipped), the immediately-preceding user
    turn (its text gathered), then the assistant turn before that (scanned for an
    ``AskUserQuestion`` tool_use). Fires only when that prior assistant turn DID
    pose an ``AskUserQuestion`` AND the intervening user turn is a clarification
    request (``_CLARIFY_REQUEST_RE``) — exactly the case the #807 gate must not
    re-gate, since the harness already routes a rejected-question clarification.
    A user who simply ANSWERED an option uses no clarify language, so this stays
    precise. Crash-proof: any odd entry contributes nothing.
    """
    # Partition the tail (newest→oldest) into the three turns of interest with a
    # role-boundary cursor: skip the trailing assistant turn, gather the
    # preceding user turn's text, then scan the prior assistant turn. Non-message
    # entries (summaries/meta) are filtered out first so they never split a turn.
    msgs = [entry for entry in reversed(entries) if _entry_message_role(entry) in {"user", "assistant"}]
    cursor = 0
    while cursor < len(msgs) and _entry_message_role(msgs[cursor]) == "assistant":
        cursor += 1
    user_texts: list[str] = []
    while cursor < len(msgs) and _entry_message_role(msgs[cursor]) == "user":
        user_texts += [
            str(block.get("text", ""))
            for block in _entry_message_blocks(msgs[cursor])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        cursor += 1
    prior_used_question = False
    while cursor < len(msgs) and _entry_message_role(msgs[cursor]) == "assistant":
        prior_used_question = prior_used_question or any(
            isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion"
            for block in _entry_message_blocks(msgs[cursor])
        )
        cursor += 1
    if not prior_used_question:
        return False
    return bool(_CLARIFY_REQUEST_RE.search("\n".join(user_texts)))


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
