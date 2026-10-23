"""Terminal question lookup and the existing same-run supersession scope."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from hooks.scripts.django_bootstrap import bootstrap_teatree_django

DECIDED_WINDOW = timedelta(days=30)


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
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — ORM after bootstrap

        for prior in DeferredQuestion.supersedable(session_id=str(data.get("session_id", "")), run_id=run_id(data)):
            prior.mark_stale("superseded by an in-client question")
    except Exception:  # noqa: BLE001 — preserve crash-proof hook behavior
        return


def resolved_terminal_question(question: str, options_hash: str) -> tuple[int, str] | None:
    if not question or not bootstrap_teatree_django():
        return None
    from django.db import connection  # noqa: PLC0415 — ORM after bootstrap

    from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — ORM after bootstrap

    introspection = connection.introspection
    if DeferredQuestion not in introspection.installed_models(introspection.table_names()):
        return None
    row = (
        DeferredQuestion.objects.decided_since(datetime.now(UTC) - DECIDED_WINDOW)
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
