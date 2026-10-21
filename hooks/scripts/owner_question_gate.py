"""Attended owner questions and final replies use one plain tool call per decision.

NEVER-LOCKOUT: audited per-action escapes for the question and final reply,
the ``[teatree] owner_question_gate_enabled = false`` kill-switch
(``t3 <overlay> gate owner-question disable``), ``_fail_open_or_deny`` and the UX
circuit breaker on the PreToolUse arm, and the ``stop_hook_active`` re-fire on Stop.
"""

import json
import os
import sys

from hooks.scripts.gate_escape import escape_allows
from hooks.scripts.managed_repo import teatree_src_on_path
from hooks.scripts.question_gates import (
    find_decision_prose,
    preceding_user_rejected_question_and_asked_clarify,
    read_transcript_entries,
)
from hooks.scripts.turn_inspect import current_turn_final_text

sys.modules.setdefault("owner_question_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.owner_question_gate", sys.modules[__name__])

HOOK_CLIENT_ENV = "T3_HOOK_CLIENT"
CODEX_CLIENT = "codex"
_QUESTION_TOOLS = frozenset(
    {
        "AskUserQuestion",
        "request_user_input",
        "request_user_input_async",
        "functions.request_user_input",
        "functions.request_user_input_async",
    }
)
_BATCHED_WARN = (
    "[teatree] The question call carried {n} questions. Ask ONE decision per call "
    "(skills/rules/references/asking-questions.md 'One decision per question'); split the rest "
    "and ask the next after this answer.\n"
)
_ATTENDED_CACHE: tuple[dict, bool] | None = None


def _dispatched_agent() -> bool:
    with teatree_src_on_path():
        from teatree.utils.agent_spawn_marker import is_dispatched_agent_process  # noqa: PLC0415 — cold-hook import

        return is_dispatched_agent_process()


def attended_turn(data: dict) -> bool:
    from hooks.scripts.hook_router import _is_live_user_turn, _session_drives_loop  # noqa: PLC0415 deferred back-import

    global _ATTENDED_CACHE  # noqa: PLW0603 — one hook invocation shares this read across handlers.
    if _ATTENDED_CACHE is not None and _ATTENDED_CACHE[0] is data:
        return _ATTENDED_CACHE[1]
    result = _is_live_user_turn(data) or not _session_drives_loop(str(data.get("session_id", "")))
    _ATTENDED_CACHE = (data, result)
    return result


def _owner_reads_this_turn(data: dict) -> bool:
    # Codex transcripts are not in the owner-prompt format, and a dispatched Codex run carries T3_DISPATCHED_AGENT.
    return os.getenv(HOOK_CLIENT_ENV) == CODEX_CLIENT or attended_turn(data)


def _gate_enabled() -> bool:
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("owner_question_gate_enabled", default=True)


def _sanctioned_prose(data: dict, text: str) -> bool:
    from hooks.scripts.hook_router import _is_classifier_relax_explanation  # noqa: PLC0415 deferred back-import

    return _is_classifier_relax_explanation(text) or preceding_user_rejected_question_and_asked_clarify(
        read_transcript_entries(str(data.get("transcript_path", "")))
    )


def handle_block_decision_prose(data: dict) -> bool | None:
    if data.get("stop_hook_active") or _dispatched_agent():
        return None
    if not _gate_enabled() or not _owner_reads_this_turn(data):
        return None
    final = data.get("last_assistant_message")
    text = (
        final
        if isinstance(final, str) and final.strip()
        else current_turn_final_text(str(data.get("transcript_path", "")))
    )
    decision = find_decision_prose(text)
    if decision is None:
        return None
    if _sanctioned_prose(data, text) or escape_allows(data, "owner-question-decision", turn_text=text):
        return None
    reason = (
        f"TEATREE OWNER DECISION GATE: {decision[:80]!r} puts an owner decision in the final reply. "
        "Raise each decision through the question tool, one decision per call, in plain words. "
        "Then end with a reply that carries no decision list. A false positive: end the reply with "
        "[gate-escape:owner-question-decision: <reason>], or run `t3 <overlay> gate owner-question disable`."
    )
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    return True


def _question_text(item: dict) -> object:
    return item.get("question", item.get("title"))


def _questions(data: dict) -> list | None:
    tool_input = data.get("tool_input")
    raw = tool_input.get("questions") if isinstance(tool_input, dict) else None
    return raw if isinstance(raw, list) else None


def _fit_problems(questions: list) -> list[str]:
    with teatree_src_on_path():
        from teatree.core.modelkit.question_card import CardView  # noqa: PLC0415 — deferred: cold-hook import

    problems: list[str] = []
    for question in questions:
        if isinstance(question, dict) and isinstance(text := _question_text(question), str) and text.strip():
            problems.extend(CardView.ask_problems({**question, "question": text}))
    return problems


def handle_block_unfit_question(data: dict) -> bool:
    if data.get("tool_name") not in _QUESTION_TOOLS or (questions := _questions(data)) is None:
        return False
    batch_count = len(questions)
    skip = _dispatched_agent() or not _gate_enabled() or not _owner_reads_this_turn(data)
    if skip:
        if batch_count > 1:
            sys.stderr.write(_BATCHED_WARN.format(n=batch_count))
        return False
    problems = [f"{batch_count} questions in one call; ask one decision per call"] if batch_count > 1 else []
    try:
        problems.extend(_fit_problems(questions))
    except ImportError:
        sys.stderr.write(
            "NOTE: the owner-question-fit gate did NOT validate wording — the shared question-card check "
            "could not be imported; remote CI cannot check the live question.\n"
        )
    if not problems:
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    reason = (
        "TEATREE OWNER QUESTION GATE: " + "; ".join(problems) + ". Ask one decision per call in plain words. "
        "A false positive: add [gate-escape:owner-question-fit: <reason>] to the question, "
        "or run `t3 <overlay> gate owner-question disable`."
    )
    return _fail_open_or_deny(data, reason, gate_id="owner-question-fit")
