"""Stop and SessionStart: deliver an answer to the session that asked it, once, with no Django.

Every answered question that names its asking session is posted to that session's mailbox in
the host-visible data dir (``teatree.answer_handback``); this claims them at the next turn end
and blocks the Stop so the session applies them rather than ending with an answer it never saw.
"""

import sys

from hooks.scripts.additional_context import emit_hook_output
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.session_start_delivery import StartClaims

sys.modules.setdefault("question_handback", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.question_handback", sys.modules[__name__])


def _collect(session_id: str) -> list:
    if not session_id:
        return []
    with teatree_src_on_path():
        from teatree import answer_handback  # noqa: PLC0415 — cold-hook import after the src bootstrap

        return answer_handback.collect(session_id)


def _lines(answers: list) -> str:
    return "\n".join(
        f'Your AskUserQuestion #{answer["id"]} was answered by the user: "{answer["answer"]}". Apply it now.'
        for answer in answers
    )


def _post_back(session_id: str, answers: list) -> None:
    """Return claimed answers to the mailbox: the write that carried them never reached the session."""
    try:
        with teatree_src_on_path():
            from teatree import answer_handback  # noqa: PLC0415 — cold-hook import after the src bootstrap

            for answer in answers:
                answer_handback.post(session_id=session_id, question_id=answer["id"], answer=answer["answer"])
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"[answer-handback] answers claimed for {session_id} were LOST, not handed back: {exc}\n")


def handle_hand_back_answers(data: dict) -> bool | None:
    session_id = str(data.get("session_id", ""))
    answers = _collect(session_id)
    if not answers:
        return None
    try:
        emit_hook_output({"decision": "block", "reason": _lines(answers)})
    except (OSError, ValueError):
        _post_back(session_id, answers)
        return None
    return True


def hand_back_context(session_id: str, claims: StartClaims) -> str:
    """The waiting answers as SessionStart context; a failure costs only the answers, never the rest of the merge."""
    try:
        answers = _collect(session_id)
    except (OSError, ImportError, RuntimeError) as exc:
        sys.stderr.write(f"[answer-handback] answers waiting for {session_id} were not read at SessionStart: {exc}\n")
        return ""
    if answers:
        claims.add(lambda: _post_back(session_id, answers))
    return _lines(answers)


__all__ = ["hand_back_context", "handle_hand_back_answers"]
