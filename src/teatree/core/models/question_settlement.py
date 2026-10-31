"""What settles an owner card the moment it is recorded: a finished subject, or a closed-form rule that decides it."""

from typing import TYPE_CHECKING

from teatree.core.modelkit.owner_stop_misfits import misfit_for
from teatree.core.models.question_subject import finished_subject_reason

if TYPE_CHECKING:
    from teatree.core.modelkit.question_card import QuestionCard
    from teatree.core.models.deferred_question import DeferredQuestion


def settle_at_record(row: "DeferredQuestion", card: "QuestionCard") -> None:
    if reason := finished_subject_reason(row):
        row.mark_stale(reason, resolver_id="record_subject_settled")
        return
    rule = misfit_for(card, row.question)
    if rule and row.apply_answer(rule.answer, resolved_via=row.ResolvedVia.POLICY):
        row.audits.create(action="answered", answer_text=rule.answer, resolver_id=f"rule:{rule.name}")
        row.refresh_from_db()
