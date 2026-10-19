"""Attended owner questions and final replies use one plain tool call per decision.

NEVER-LOCKOUT: the per-call ``[owner-question-ok: <reason>]`` token (question text or
final reply), the ``[teatree] owner_question_gate_enabled = false`` kill-switch
(``t3 <overlay> gate owner-question disable``), ``_fail_open_or_deny`` and the UX
circuit breaker on the PreToolUse arm, and the ``stop_hook_active`` re-fire on Stop.
"""

import json
import os
import re
import sys

from hooks.scripts.gate_result import warn_gate_skipped
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
_ESCAPE_RE = re.compile(r"\[owner-question-ok:\s*(\S[^\]]*?)\s*\]")
_ESCAPE_SCAN_CHARS = 512
_BATCHED_WARN = (
    "[teatree] The question call carried {n} questions. Ask ONE decision per call "
    "(skills/rules/references/asking-questions.md 'One decision per question'); split the rest "
    "and ask the next after this answer.\n"
)


def attended_turn(data: dict) -> bool:
    from hooks.scripts.hook_router import _is_live_user_turn, _session_drives_loop  # noqa: PLC0415 deferred back-import

    return _is_live_user_turn(data) or not _session_drives_loop(str(data.get("session_id", "")))


def _owner_reads_this_turn(data: dict) -> bool:
    # Codex transcripts are not in the owner-prompt format, and a dispatched Codex run carries T3_DISPATCHED_AGENT.
    return os.getenv(HOOK_CLIENT_ENV) == CODEX_CLIENT or attended_turn(data)


def _gate_enabled() -> bool:
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("owner_question_gate_enabled", default=True)


def _escape_reason(texts: list[str]) -> str | None:
    reason = next((match[1] for text in texts if (match := _ESCAPE_RE.search(text[:_ESCAPE_SCAN_CHARS]))), None)
    if reason:
        sys.stderr.write(f"NOTE: owner-question gate skipped via [owner-question-ok: {reason}].\n")
    return reason


def _sanctioned_prose(data: dict, text: str) -> bool:
    from hooks.scripts.hook_router import _is_classifier_relax_explanation  # noqa: PLC0415 deferred back-import

    return _is_classifier_relax_explanation(text) or preceding_user_rejected_question_and_asked_clarify(
        read_transcript_entries(str(data.get("transcript_path", "")))
    )


def handle_block_decision_prose(data: dict) -> bool | None:
    if data.get("stop_hook_active") or os.getenv("T3_DISPATCHED_AGENT"):
        return None
    final = data.get("last_assistant_message")
    text = (
        final
        if isinstance(final, str) and final.strip()
        else current_turn_final_text(str(data.get("transcript_path", "")))
    )
    decision = find_decision_prose(text)
    if decision is None or _escape_reason([text[-_ESCAPE_SCAN_CHARS:]]) or not _gate_enabled():
        return None
    if not _owner_reads_this_turn(data) or _sanctioned_prose(data, text):
        return None
    reason = (
        f"TEATREE OWNER DECISION GATE: {decision[:80]!r} puts an owner decision in the final reply. "
        "Raise each decision through the question tool, one decision per call, in plain words. "
        "Then end with a reply that carries no decision list. A false positive: end the reply with "
        "[owner-question-ok: <reason>], or run `t3 <overlay> gate owner-question disable`."
    )
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    return True


def _question_text(item: dict) -> object:
    return item.get("question", item.get("title"))


def _questions(data: dict) -> list | None:
    tool_input = data.get("tool_input")
    raw = tool_input.get("questions") if isinstance(tool_input, dict) else None
    return raw if isinstance(raw, list) else None


def _well_formed(questions: list) -> bool:
    return all(
        isinstance(item, dict) and isinstance(text := _question_text(item), str) and text.strip() for item in questions
    )


def _fit_problems(questions: list[dict]) -> list[str]:
    problems = [f"{len(questions)} questions in one call; ask one decision per call"] if len(questions) > 1 else []
    try:
        with teatree_src_on_path():
            from teatree.core.modelkit.question_card import CardView  # noqa: PLC0415 — deferred: cold-hook import
    except ImportError:
        warn_gate_skipped("owner-question-fit", "the shared question-card check could not be imported")
        return problems
    for question in questions:
        problems.extend(CardView.ask_problems({**question, "question": _question_text(question)}))
    return problems


def handle_block_unfit_question(data: dict) -> bool:
    if data.get("tool_name") not in _QUESTION_TOOLS or (questions := _questions(data)) is None:
        return False
    problems = _fit_problems(questions) if _well_formed(questions) else []
    if (
        not problems
        or os.getenv("T3_DISPATCHED_AGENT")
        or _escape_reason([str(_question_text(question)) for question in questions])
        or not _gate_enabled()
        or not _owner_reads_this_turn(data)
    ):
        if len(questions) > 1:
            sys.stderr.write(_BATCHED_WARN.format(n=len(questions)))
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    reason = (
        "TEATREE OWNER QUESTION GATE: " + "; ".join(problems) + ". Ask one decision per call in plain words. "
        "A false positive: add [owner-question-ok: <reason>] to the question, "
        "or run `t3 <overlay> gate owner-question disable`."
    )
    return _fail_open_or_deny(data, reason, gate_id="owner-question-fit")
