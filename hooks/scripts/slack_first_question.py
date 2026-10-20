"""PreToolUse ``AskUserQuestion`` routing helpers shared by the attended and the loop-driven arms.

Split out of ``hook_router`` (the shrink-only god-module): the run id that scopes a question's supersession
and the supersession itself, so the Slack-first arm and the router's own mirror handler read one answer.
"""

import sys

from hooks.scripts.django_bootstrap import bootstrap_teatree_django

sys.modules.setdefault("slack_first_question", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.slack_first_question", sys.modules[__name__])


def supersede_pending_questions(data: dict) -> None:
    """Stale-mark this (session, run)'s pending rows — an in-client ask replaces them.

    A loop-driven row the agent later re-asks on an attended turn would otherwise keep
    nagging from ``reask_escalated_questions`` after the owner has already answered.

    Scoped by ``supersedable``: a raw ``pending()`` filter dismisses a DELIVERED row a Slack reply binds to,
    sweeps the INTERNAL health queue, and — on the common run-less payload — erases the session's backlog (#4721).
    """
    if not bootstrap_teatree_django():
        return
    try:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — deferred: ORM

        for prior in DeferredQuestion.supersedable(
            session_id=str(data.get("session_id", "")), run_id=question_run_id(data)
        ):
            prior.mark_stale("superseded by an in-client question")
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure degrades silently, never breaks the tool call
        return


def question_run_id(data: dict) -> str:
    """Harness run id when the payload exposes one; empty when it names no run.

    The (session, run) pair scopes the generation cursor and what a newer question may supersede; empty rather
    than the session id, whose fallback widened that scope to the session's whole backlog (#4721).
    """
    for key in ("run_id", "agent_run_id", "tool_use_id"):
        value = str(data.get(key, "")).strip()
        if value:
            return value
    return ""
