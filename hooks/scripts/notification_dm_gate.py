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
The transcript-shape detection itself lives in the ``teatree.hooks.stalled_ask_detect``
leaf (lazily imported inside the ``managed_repo.teatree_src_on_path`` ``src/``
bootstrap, #1314) — shared with the ``t3 doctor check`` stalled-ask finding, so the
Notification DM and the doctor finding can never disagree about what "still pending"
means.
"""

import contextlib
import hashlib
import os

from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.t3_invocation import spawn_t3_detached, t3_argv

_STALLED_ASK_REASON = "stalled-ask"


def _pending_ask_text(transcript_path: str) -> str | None:
    """The question text of a trailing, still-blocking ``AskUserQuestion`` call.

    Delegates to :func:`teatree.hooks.stalled_ask_detect.pending_ask_text`. Fail-safe:
    an unimportable ``teatree`` or any internal error is ``None`` here too, same as
    every other shape that predicate already treats as "not pending".
    """
    try:
        with teatree_src_on_path():
            from teatree.hooks.stalled_ask_detect import pending_ask_text  # noqa: PLC0415 — deferred: cold-hook import

            return pending_ask_text(transcript_path)
    except Exception:  # noqa: BLE001 — crash-proof hook: an unreadable transcript is not pending
        return None


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
