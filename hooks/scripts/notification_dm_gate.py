"""Notification hook: DM the owner when a terminal is parked on an unanswered ask (#4818).

AskUserQuestion is not itself one of the Notification hook's matchers
(``idle_prompt`` / ``permission_prompt`` / ``agent_needs_input`` — Claude Code's own
docs), and no ``Notification`` event was registered at all before this, so a session
that blocked on the tool had nothing to surface it: it just idled, invisible, until a
human happened to attach to that terminal. Every one of those notification kinds means
"this terminal needs you", so rather than gate on an unconfirmed sub-type field, this
handler asks a narrower, verifiable question of the transcript itself: does the LAST
entry hold an ``AskUserQuestion`` ``tool_use`` with no ``tool_result`` after it — i.e. a
call that is genuinely still blocking? Firing on that shape (whatever the Notification
sub-type) is precise without depending on a payload field outside this repo's control.

A bare sibling module (``hook_router`` is a shrink-only god-module at its LOC cap):
this concern lives here and is imported back into the router's ``_HANDLERS`` chain.
"""

import contextlib
import hashlib
import os

from hooks.scripts.question_gates import read_transcript_entries
from hooks.scripts.t3_invocation import spawn_t3_detached, t3_argv

_STALLED_ASK_REASON = "stalled-ask"


def _pending_ask_text(transcript_path: str) -> str | None:
    """The question text of a trailing, still-blocking ``AskUserQuestion`` call.

    "Trailing" means the transcript's LAST entry is the assistant's own ``tool_use``
    block, with nothing after it — exactly the shape while the call genuinely blocks
    on the owner (a ``tool_result`` lands only once it returns). Fail-safe: any other
    shape (already answered, no ask posed, an unreadable/empty transcript) is ``None``.
    """
    entries = read_transcript_entries(transcript_path)
    if not entries:
        return None
    last = entries[-1]
    message = last.get("message") if isinstance(last, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or not content:
        return None
    block = content[-1]
    if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "AskUserQuestion":
        return None
    tool_input = block.get("input")
    questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
    first = questions[0] if isinstance(questions, list) and questions else {}
    text = str(first.get("question", "")).strip() if isinstance(first, dict) else ""
    return text or None


def _stalled_ask_idempotency_key(session_id: str, question_text: str) -> str:
    """Stable per-(session, question) key.

    A repeat Notification for the SAME still-pending ask must not re-DM every poll;
    ``notify send``'s own dedupe enforces that from this key alone, so no separate
    marker file is needed here.
    """
    digest = hashlib.sha256(question_text.encode("utf-8")).hexdigest()[:16]
    return f"{_STALLED_ASK_REASON}-{session_id}-{digest}"


def handle_notify_stalled_ask(data: dict) -> None:
    """Notification: DM the owner naming the session, cwd, and the pending question.

    Fires for any Notification sub-type; the actual gate is ``_pending_ask_text``
    finding a genuinely-blocking ``AskUserQuestion`` in the transcript, so an
    unrelated notification (an auth prompt, a quota resume) sends nothing. Crash-proof
    and fail-safe throughout: a missing overlay, an unresolvable ``t3`` argv, or any
    internal error is a silent no-op — this hook can only ever add a DM, never block.
    """
    question_text = _pending_ask_text(str(data.get("transcript_path", "")))
    if not question_text:
        return
    session_id = str(data.get("session_id", ""))
    cwd = str(data.get("cwd", ""))
    body = f"Session `{session_id}` is waiting on you" + (f" in `{cwd}`" if cwd else "") + f":\n> {question_text}"
    overlay = os.environ.get("T3_OVERLAY_NAME", "")
    if not overlay:
        return
    argv = t3_argv(
        overlay.removeprefix("t3-"),
        "notify",
        "send",
        body,
        "--idempotency-key",
        _stalled_ask_idempotency_key(session_id, question_text),
        "--kind",
        "question",
        "--overlay",
        overlay,
    )
    if argv is None:
        return
    with contextlib.suppress(Exception):
        spawn_t3_detached(argv)
