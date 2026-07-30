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

# Between the announce head and "ask", a BOUNDED filler run is tolerated ("I'll
# GO AHEAD AND ask ...", "let me QUICKLY ask ..." -- the metered evasion shape).
# A closed WHITELIST, never a \w+ run: an open run would carry "I will NOT
# ask ..." (a negated resolution) and "I did NOT just ask ..." (a past-tense
# report) into the wire -- the negation-guarded false positives this design
# promises never fire. "going to" rides the SAME single run ("I'm JUST GOING TO
# ask ..."): a second starred run around an optional segment backtracks O(n^2)
# on a degenerate token repetition and blows the 30s Stop-hook budget, so there
# is exactly one run, hard-bounded at 4 repetitions.
_FILLER_WORD = r"(?:go ahead and|proceed to|just|now|simply|quickly|briefly|first|then|directly|going to)"
_ASK_FILLER = rf"(?:\s+{_FILLER_WORD}){{0,4}}"

# The line-start lead drops "first"/"then": with no first-person or action-word
# anchor, a standalone instructional line ("Then ask the user for the deployed
# URL ...") is third-party guidance, not an announced ask. Post-anchor runs keep
# the full vocabulary.
_LEAD_FILLER_WORD = r"(?:go ahead and|proceed to|just|now|simply|quickly|briefly|directly|going to)"
_ASK_FILLER_LEAD = rf"(?:{_LEAD_FILLER_WORD}\s+){{0,4}}"
_ANNOUNCED_ASK_RE = re.compile(
    rf"\bi(?:(?:'|\u2019)?ll| will|(?:'|\u2019)?m| am(?: about to)?| need to| must"
    rf"| should| have to| want to)?{_ASK_FILLER} ask {_ASK_OBJECT}\b"
    rf"|\b(?:let me|time to){_ASK_FILLER} ask {_ASK_OBJECT}\b"
    rf"|\b(?:action|next(?: step)?|now|plan|step \d+)\b\W{{0,4}}{_ASK_FILLER_LEAD}ask {_ASK_OBJECT}\b"
    rf"|^(?:[-*>\u2022]\s{{0,2}}|\*\*)?{_ASK_FILLER_LEAD}ask {_ASK_OBJECT}\b",
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

# A RENDERED tool-call chip — the model mimics the chat-UI RENDERING of an
# AskUserQuestion call as text: a standalone emphasized tool-name line
# ("**AskUserQuestion**", "**AskUserQuestion:**", "**AskUserQuestion — PR #1
# merge decision**"), optionally footnoted with a UI marker line ("*View tool
# call*", "*(1 more tool call)*"), with ZERO real tool calls in the turn — the
# four observed 2026-07-08 metered reds of
# `structured_question_one_decision_per_question`, verbatim. Scanned RAW (a
# fenced fake chip is still a fake chip) and never suppressed by the
# pending-answer disposition, like printed call syntax. The chip line must be
# ONLY the emphasis-wrapped tool name — bare, or delimiter-titled with ":",
# an em/en dash, or a whitespace-set ASCII dash — so an inline prose mention
# ("the AskUserQuestion for the branch decision was captured as
# DeferredQuestion #4"), a bold rule statement ("**AskUserQuestion for every
# decision** is the rule" — no delimiter), a compound coinage
# ("**AskUserQuestion-based routing**" — no whitespace around the dash), and
# a markdown heading ("## AskUserQuestion" — no emphasis wrap) never fire.
# The footnote marker is corroboration, not an independent trigger: with the
# tool name anywhere in the turn it also catches an UNemphasized name line,
# but a bare "View tool call" line with no tool name never fires. Scoped out
# (no observed terminal): a lone bare unemphasized "AskUserQuestion" line
# with no footnote, and a single-line chip+footnote combination.
_RENDERED_CHIP_NAME_RE = re.compile(
    r"^[ \t>]*[*_`]{1,3}AskUserQuestion(?:(?:\s*[:\u2014\u2013]|\s--?\s)[^\n]*)?[*_`]{1,3}[.:]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_RENDERED_CHIP_FOOTNOTE_RE = re.compile(
    r"^[ \t>]*[*_`]{0,3}\(?\s*(?:view tool call|\d+ more tool calls?)\s*\)?[*_`]{0,3}[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


def _rendered_tool_chip(text: str) -> bool:
    if _RENDERED_CHIP_NAME_RE.search(text):
        return True
    return bool(_RENDERED_CHIP_FOOTNOTE_RE.search(text) and "AskUserQuestion" in text)


# The #807 Stop-gate block reason — owned here beside the detection heuristic it
# explains (the shrink-only router imports it back).
STRUCTURED_QUESTION_BLOCK = (
    "TEATREE GATE — a user-directed question was posed inline in prose (or "
    "announced/printed/rendered — 'I'll ask…' / a literal AskUserQuestion(...) "
    "line / a '**AskUserQuestion**' tool-call chip drawn as text — without "
    "being issued) with no AskUserQuestion tool call in this turn. "
    "Inline/narrated questions are invisible in autonomous/loop runs (they read "
    "as log lines) so the decision is lost. Issue a REAL AskUserQuestion tool "
    "call NOW — an actual tool invocation, never call syntax or a rendered "
    "chip printed as text — one question at a time, with concrete options — "
    "then continue. This is a non-bypassable gate (no `relax:` escape): the "
    "question must go through the structured tool."
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


# A '?' that survives ONLY inside a quoted span or a blockquote line is an
# ECHOED question, not a live ask (#3261): the agent is quoting an earlier
# question back — e.g. answering the user's meta-question "what was the last
# question you asked me?" by reproducing the prior question verbatim. Single-quote
# spans are deliberately NOT stripped (an apostrophe in a contraction has no close
# and a two-contraction line would eat the span between them); a quoted question
# is virtually always double-quoted or backticked.
_QUOTED_SPAN_RE = re.compile(r'"[^"\n]*"|`[^`\n]*`')
_BLOCKQUOTE_LINE_RE = re.compile(r"^[ \t]*>[^\n]*", re.MULTILINE)

# A PAST-tense attribution that introduces a HISTORICAL question the turn merely
# recounts ("the last question I asked was …", "you asked whether …") rather than
# a live decision (#3261). The clause is bounded to its own sentence (``[^.!?\n]*``
# up to the ``?``) so only the attributed interrogative is neutralised — a genuine
# live question elsewhere in the same turn survives and still fires the gate.
_HISTORICAL_QUESTION_RE = re.compile(
    r"\b(?:"
    r"the\s+(?:last|previous|earlier|prior)\s+question\s+(?:i|we)\s+(?:asked|posed|raised)"
    r"|(?:the\s+)?question\s+(?:i|we)\s+(?:asked|posed|raised)"
    r"|you\s+(?:previously|earlier|already|just)?\s*asked"
    r"|(?:i|we)\s+(?:previously|earlier|already|just)\s+asked"
    r"|(?:my|our)\s+(?:last|previous|earlier|prior)\s+question"
    r")\b[^.!?\n]*\?",
    re.IGNORECASE,
)


def _question_is_only_quoted_or_historical(prose: str) -> bool:
    """True when every live-question signal is quoted or attributed to the past (#3261).

    Strips quoted spans, blockquote lines, and historical-attribution clauses,
    then re-checks for a surviving user-directed ``?``. When none survives, the
    turn merely quotes/recounts a prior question — not a live decision — so the
    gate must not force a spurious ``AskUserQuestion``. A genuine live question
    survives the strip and keeps firing.
    """
    cleaned = _QUOTED_SPAN_RE.sub(" ", prose)
    cleaned = _BLOCKQUOTE_LINE_RE.sub(" ", cleaned)
    cleaned = _HISTORICAL_QUESTION_RE.sub(" ", cleaned)
    if "?" not in cleaned:
        return True
    return not _USER_DIRECTED_CUE_RE.search(cleaned)


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

    A QUOTED or HISTORICAL question does NOT fire (#3261): when the only
    user-directed ``?`` survives inside a quoted span / blockquote (the agent
    quoting an earlier question back — e.g. answering "what was the last question
    you asked me?") or inside a past-attribution clause ("the last question I
    asked was …", "you asked whether …"), there is no live decision to route, so
    forcing an ``AskUserQuestion`` would manufacture a spurious round-trip. A
    genuine live question outside such spans still fires.

    An ANNOUNCED ask ("**Action:** Ask about the first PR", "I'll ask the user
    which branch" — a bounded filler run between the head and "ask" included:
    "I'll go ahead and ask …"), a PRINTED tool call (`AskUserQuestion(...)`
    emitted as text, fenced or inline), and a RENDERED tool-call chip
    ("**AskUserQuestion**" drawn as a standalone text line, optionally with a
    "*View tool call*" footnote) trip independently of the ``?`` requirement
    too — narrating, printing, or drawing the ask is not asking, and on a loop
    turn the decision is silently lost. The one-ask-then-wait disposition
    ("once you answer, I'll ask the second decision", with evidence something
    was asked) suppresses only the announced-ask wire so a compliant
    walk-through is never re-ask-looped; printed call syntax and a rendered
    chip are wrong in every posture and are never suppressed.
    """
    prose = FENCED_CODE_RE.sub(" ", text)
    if _SOFT_ASK_CUE_RE.search(prose):
        return True
    if _PRINTED_TOOL_CALL_RE.search(text) or _rendered_tool_chip(text):
        return True
    if _ANNOUNCED_ASK_RE.search(prose) and not _pending_answer_disposition(prose):
        return True
    if "?" not in prose or not _USER_DIRECTED_CUE_RE.search(prose):
        return False
    if _SEQUENCING_OFFER_RE.search(prose):
        return False
    return not _question_is_only_quoted_or_historical(prose)


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


def _answer_text_from_tool_response(data: dict) -> str:
    """The user's in-client choice, or a neutral marker when the shape is unfamiliar.

    The harness's ``AskUserQuestion`` response shape is not contractual, so this reads
    the common carriers and otherwise records that the question WAS answered here. The
    exact text matters less than the resolution: an unresolved row keeps pinging Slack.
    """
    response = data.get("tool_response")
    if isinstance(response, str) and response.strip():
        return response.strip()
    if isinstance(response, dict):
        for key in ("answer", "choice", "label", "response", "text"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return "answered in session"


def handle_resolve_answered_question(data: dict) -> None:
    """PostToolUse: close the mirrored ``DeferredQuestion`` once the user answers in-client (#3642).

    The interactive arm of the router's ``handle_mirror_question_to_slack`` records
    every question so it is answerable from Slack. That makes an in-client answer a
    resolution the Slack side must see: without this the row would stay pending, keep
    binding Slack replies, and be re-raised by the resurfacing side after the owner had
    already answered. Matches on the harness ``tool_use_id`` — the same identifier the
    capture stored — so it can never resolve a different question.

    Crash-proof and best-effort, like every hook: an unavailable teatree, an unmatched
    id, or an already-resolved row all degrade to doing nothing.
    """
    if data.get("tool_name") != "AskUserQuestion":
        return
    tool_use_id = str(data.get("tool_use_id", "")).strip()
    if not tool_use_id:
        return
    try:
        from hooks.scripts.django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 deferred cold-hook import

        if not bootstrap_teatree_django():
            return
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — deferred: ORM/app-registry

        row = DeferredQuestion.objects.filter(
            tool_use_id=tool_use_id, answered_at__isnull=True, dismissed_at__isnull=True
        ).first()
        if row is not None:
            row.apply_answer(_answer_text_from_tool_response(data), resolved_via=DeferredQuestion.ResolvedVia.LOCAL)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return
