"""Terminal question lookup and the existing same-run supersession scope."""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib import import_module

from hooks.scripts.django_bootstrap import bootstrap_teatree_django

DECIDED_WINDOW = timedelta(days=30)
_logger = logging.getLogger(__name__)


def run_id(data: Mapping[str, object]) -> str:
    for key in ("run_id", "agent_run_id", "tool_use_id"):
        value = str(data.get(key, "")).strip()
        if value:
            return value
    return ""


def supersede_pending_questions(data: Mapping[str, object]) -> None:
    if not bootstrap_teatree_django():
        return
    try:
        question_model = import_module("teatree.core.models.deferred_question").DeferredQuestion

        for prior in question_model.supersedable(session_id=str(data.get("session_id", "")), run_id=run_id(data)):
            prior.mark_stale("superseded by an in-client question")
    except Exception:
        _logger.exception("Question supersession failed")
        return


def lookup_terminal_question(question: str, options_hash: str) -> tuple[int, str] | None:
    try:
        return resolved_terminal_question(question, options_hash)
    except Exception:
        _logger.exception("Question ledger read failed")
        return None


def resolved_terminal_question(question: str, options_hash: str) -> tuple[int, str] | None:
    if not question or not bootstrap_teatree_django():
        return None
    connection = import_module("django.db").connection
    question_model = import_module("teatree.core.models.deferred_question").DeferredQuestion

    introspection = connection.introspection
    if question_model not in introspection.installed_models(introspection.table_names()):
        return None
    row = (
        question_model.objects.decided_since(datetime.now(UTC) - DECIDED_WINDOW)
        .filter(question=question, options_hash=options_hash)
        .order_by("-created_at", "-pk")
        .first()
    )
    if row is None:
        return None
    disposition = (
        f"answered: {row.answer_text}" if row.answered_at is not None else f"dismissed: {row.dismissed_reason}"
    )
    return row.pk, disposition
