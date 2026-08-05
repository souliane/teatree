"""Stop: answer-first gate — a user's question is answered, not actioned.

The counterpart the structured-question gate (#807) never had. That gate fires
when the AGENT poses a question without the structured tool; nothing fired on the
inverse — the USER asks and the agent replies with a delegation report, so the
owner has to ask the same thing twice. Acting is not answering, and when someone
asks *why*, work is not a substitute for an explanation.

Unlike the sibling Stop gates this one does NOT skip an attended turn. Their
reason for skipping is that a human is reading the prose, which is exactly the
condition under which THIS failure hurts: the human asked, and is waiting. The
precision comes instead from the detector's three-way conjunction (a question, a
delegation report, no answer) rather than from the turn's audience.

Never-lockout: ``stop_hook_active`` short-circuits the re-fire, the per-call
``[skip-answer-gate: <reason>]`` token in the turn text clears one stop, the
``[teatree] answer_first_gate_enabled = false`` kill-switch
(``t3 <overlay> gate answer-first disable``) clears all of them, and any internal
error allows the stop — a Stop hook must never crash turn-end.
"""

import contextlib
import json
import re
import sys
from pathlib import Path

# Alias both identities so the live hook's bare import and a test's
# ``hooks.scripts.answer_first_gate`` import resolve ONE module object.
sys.modules.setdefault("answer_first_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.answer_first_gate", sys.modules[__name__])

_SKIP_TOKEN_RE = re.compile(r"\[skip-answer-gate:\s*(\S[^\]]*?)\s*\]")

# The loop injects the agent's OWN unanswered questions into the next user turn
# so it does not re-ask them. They are second-person by construction ("#12 — Do
# you want me to merge PR #41?"), so without this strip every tick carrying a
# backlog reads as the user having just asked something.
_DEFERRED_BACKLOG_RE = re.compile(
    r"^You have \d+ deferred question\(s\) awaiting user answer:\n(?:[ \t]+#\d+ .*(?:\n|$))*",
    re.MULTILINE,
)

# Material the user pasted rather than typed. A question inside a quoted mail or
# a fenced excerpt was asked by that document's author, so it is not an ask the
# agent owes an answer for; without this strip, "here is the review, please
# apply it" carrying "> why was this not caught?" reads as the user asking.
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_QUOTED_BLOCK_RE = re.compile(r"^[ \t]*>.*(?:\n|$)", re.MULTILINE)


def _gate_enabled() -> bool:
    from hooks.scripts.teatree_settings import teatree_bool_setting  # noqa: PLC0415 deferred cold-hook import

    return teatree_bool_setting("answer_first_gate_enabled", default=True)


def _skip_token(text: str) -> str | None:
    match = _SKIP_TOKEN_RE.search(text)
    if match is None:
        return None
    return match.group(1).strip() or None


def _user_words_only(text: str) -> str:
    """*text* with everything the user did not actually say removed.

    Three sources ride into the same message and all read as asks: the harness
    ambient wrappers (stripped by the shared #1567 helper, so this gate and the
    skill loader agree on what counts as genuine intent), the loop's own
    deferred-question backlog, and material the user PASTED rather than wrote —
    a quoted mail, a spec excerpt, a fenced log — where a '?' belongs to the
    document's author, not to the asker. A Slack reply relayed into the turn IS
    the user speaking and is deliberately kept.
    """
    from hooks.scripts.skill_loader_input import strip_ambient_context  # noqa: PLC0415 deferred cold-hook import

    prose = strip_ambient_context(text)
    for pattern in (_FENCED_BLOCK_RE, _QUOTED_BLOCK_RE, _DEFERRED_BACKLOG_RE):
        prose = pattern.sub(" ", prose)
    return prose


def _last_user_text(transcript_path: str) -> str:
    """What the user actually said in the most recent user message, else ``""``.

    Walks the transcript newest-first to the most recent GENUINE user message.
    Stopping at the first ``user`` entry is not enough: a tool result is recorded
    as a ``user`` entry whose blocks are all ``tool_result``, so any turn that
    called a tool — which is EVERY delegation-report turn, the exact shape this
    gate exists for — hides the real question behind one, and the text filter
    then yields ``""``. Those entries are walked past. Any odd entry contributes
    nothing rather than raising.
    """
    from hooks.scripts.question_gates import (  # noqa: PLC0415 deferred cold-hook import
        is_tool_result_only,
        read_transcript_entries,
    )

    for entry in reversed(read_transcript_entries(transcript_path)):
        message = entry.get("message")
        role = message.get("role") if isinstance(message, dict) else entry.get("type")
        if role != "user":
            continue
        content = message.get("content", []) if isinstance(message, dict) else []
        if isinstance(content, str):
            return _user_words_only(content)
        if not isinstance(content, list):
            return ""
        if is_tool_result_only(content):
            continue
        return _user_words_only(
            "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        )
    return ""


def handle_answer_first_gate(data: dict) -> bool | None:
    """Block a Stop whose final turn delegates instead of answering the user's question.

    Returns ``True`` (emitting a ``decision: block``) only when the detector fires
    on the last user message and the final assistant turn. Otherwise returns
    ``None`` so the session may end normally. Fail-safe-to-silent on any error.
    """
    src_dir = Path(__file__).resolve().parents[2] / "src"
    added = False
    try:
        if str(src_dir) not in sys.path:
            sys.path.insert(0, str(src_dir))
            added = True
        return _run(data)
    except Exception:  # noqa: BLE001 — Stop hook must be crash-proof
        return None
    finally:
        if added:
            with contextlib.suppress(ValueError):
                sys.path.remove(str(src_dir))


def _run(data: dict) -> bool | None:
    from hooks.scripts.hook_router import _last_assistant_turn  # noqa: PLC0415 deferred back-import
    from teatree.hooks import answer_first_scanner  # noqa: PLC0415 — deferred: cold-hook import

    if data.get("stop_hook_active") or not _gate_enabled():
        return None
    transcript_path = data.get("transcript_path", "")
    turn = _last_assistant_turn(transcript_path)
    if turn is None:
        return None
    agent_text = turn[0]
    if reason := _skip_token(agent_text):
        sys.stderr.write(f"NOTE: answer-first gate skipped via [skip-answer-gate: {reason}].\n")
        return None
    verdict = answer_first_scanner.find_unanswered_question(_last_user_text(transcript_path), agent_text)
    if verdict is None:
        return None
    json.dump({"decision": "block", "reason": answer_first_scanner.format_block_message(verdict)}, sys.stdout)
    return True
