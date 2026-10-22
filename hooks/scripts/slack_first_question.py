"""PreToolUse ``AskUserQuestion`` routing for an attended session (directive 5).

Split out of ``hook_router`` (the shrink-only god-module). While factory Slack is live an attended
session's question is recorded as an owner question and posted to the owner's factory DM, and the in-client
modal is denied; otherwise it renders in-client and supersedes the loop row it replaces. Any failure renders
the modal: the question is never lost to a broken Slack path.
"""

import os
import re
import sys

from hooks.scripts.classifier_relax_gate import ask_input_offers_relax_option
from hooks.scripts.django_bootstrap import bootstrap_teatree_django
from hooks.scripts.question_gates import _first_question, denied_question_dedupe_key, denied_question_row_marker

SLACK_FIRST_QUESTION_GATE = "slack_first_question"
_RECOMMENDED_SUFFIX = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)

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


def route_attended_question_to_slack(data: dict) -> str | None:
    """The deny reason once the question was recorded for the owner's factory DM; ``None`` to render the modal."""
    if not bootstrap_teatree_django():
        return None
    try:
        return _route(data)
    except Exception:  # noqa: BLE001 — crash-proof hook: any failure renders the question in-client
        return None


def _route(data: dict) -> str | None:
    from teatree.core.modelkit.owner_decision import OwnerDecision  # noqa: PLC0415 — deferred: cold-hook import
    from teatree.core.modelkit.question_card import CardOption, QuestionCard  # noqa: PLC0415 — deferred: cold-hook
    from teatree.core.models.deferred_question import (  # noqa: PLC0415 — deferred: ORM
        DeferredQuestion,
        DeferredQuestionError,
    )
    from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions  # noqa: PLC0415 — deferred
    from teatree.core.slack_first.factory_slack_liveness import factory_slack_live  # noqa: PLC0415 — deferred: Django

    verdict = factory_slack_live(os.environ.get("T3_OVERLAY_NAME", ""))
    first = _first_question(data)
    question = str(first.get("question", "")).strip()
    if not verdict.live or not question:
        return None
    raw_options = first.get("options")
    options = (
        tuple(option for item in raw_options if (option := CardOption.from_raw(item)))
        if isinstance(raw_options, list)
        else ()
    )
    options = tuple(
        CardOption(_RECOMMENDED_SUFFIX.sub("", option.label), option.description, recommended=index == 0)
        for index, option in enumerate(options)
    )
    card = QuestionCard(
        decision=OwnerDecision.CREDENTIALS
        if ask_input_offers_relax_option(data.get("tool_input"))
        else OwnerDecision.PRODUCT_SCOPE,
        checked=("Your session asked this while factory Slack is live, so it is posted here.",),
        blocker="Only you can choose.",
        why="Your session is waiting on this answer before it carries on.",
        options=options,
    )
    session_id, run_id = str(data.get("session_id", "")), question_run_id(data)
    try:
        row = DeferredQuestion.record(
            question,
            session_id=session_id,
            tool_use_id=str(data.get("tool_use_id", "")),
            generation=DeferredQuestion.next_generation(session_id=session_id, run_id=run_id),
            run_id=run_id,
            dedupe_marker=denied_question_row_marker(session_id, denied_question_dedupe_key(first)),
            card=card,
        )
    except DeferredQuestionError:
        return None
    drain_unmirrored_deferred_questions(user_id=verdict.owner_id, overlay=verdict.overlay, backend=verdict.backend)
    row.refresh_from_db()
    where = "posted to the owner's factory DM" if row.slack_ts else "queued for the owner's factory DM"
    return (
        f"Factory Slack is live, so your question is {where} as DeferredQuestion #{row.pk} "
        f"({SLACK_FIRST_QUESTION_GATE}), not shown here. Do not ask it again: do what you can without the answer, "
        "or end the turn; the owner's reply reaches you through Slack."
    )
