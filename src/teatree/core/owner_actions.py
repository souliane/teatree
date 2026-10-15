"""Carry out an owner's Slack answer: recheck the live evidence first, then write once under the lock."""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from django.db import transaction

from teatree.core.merge.void_merge import VoidRefusal, VoidRefusedError, approve_owner_void
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker, read_action
from teatree.core.models import Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.task_handoff import release_checkout_park
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.worktree.checkout_attest import AttestRefusedError, read_attestation

logger = logging.getLogger(__name__)
_OUTCOMES = ("carried_out", "declined", "refused")
_LABELS = {OwnerActionKind.ATTEST: ("accept", "decline"), OwnerActionKind.VOID: ("void", "keep")}

type _Write = Callable[[], str]


class OwnerActionDeferredError(Exception):
    pass


class OwnerActionRefusedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Decision:
    outcome: str
    note: str = ""
    notify: bool = False
    write: _Write | None = None


def _prepare_attest(row: DeferredQuestion, action: ActionRequest, _actor: str) -> _Write:
    worktree = Worktree.objects.filter(pk=action["target"]).first() if action["target"].isdigit() else None
    if worktree is None:
        message = "The checkout no longer has a record."
        raise OwnerActionRefusedError(message)
    try:
        attestation = read_attestation(worktree, expected_digest=action["binding"])
    except AttestRefusedError as exc:
        message = f"The checkout {exc}"
        if exc.retryable:
            raise OwnerActionDeferredError(message) from exc
        raise OwnerActionRefusedError(message) from exc

    def write() -> str:
        attestation.record()
        if row.parked_task is not None:
            release_checkout_park(row.parked_task)
        return f"Attested the checkout at {attestation.state.digest}."

    return write


def _prepare_void(_row: DeferredQuestion, action: ActionRequest, actor: str) -> _Write:
    try:
        approved = approve_owner_void(
            action["target"], binding=action["binding"], reason=action.get("note", ""), actor=actor
        )
    except VoidRefusedError as exc:
        if exc.code == VoidRefusal.FORGE_UNREADABLE:
            raise OwnerActionDeferredError(exc.message) from exc
        raise OwnerActionRefusedError(exc.message) from exc
    return lambda: f"Voided merge record {approved.write().pk}."


_PREPARE: dict[OwnerActionKind, Callable[[DeferredQuestion, ActionRequest, str], _Write]] = {
    OwnerActionKind.ATTEST: _prepare_attest,
    OwnerActionKind.VOID: _prepare_void,
}


def _answered_by(row: DeferredQuestion) -> str:
    answers = DeferredQuestionAudit.objects.filter(question=row, action="answered", resolver_id__startswith="slack:")
    answer = answers.order_by("-pk").first()
    return answer.resolver_id if answer is not None else ""


def _decide(row: DeferredQuestion, actor: str) -> _Decision:
    action = read_action(row.evidence)
    if action is None or row.dedupe_marker != action_marker(action):
        return _Decision("refused", "The action card no longer matches its recorded request.", notify=True)
    if row.resolved_via != DeferredQuestion.ResolvedVia.SLACK or not actor:
        return _Decision("refused", "Only an authenticated owner answer in Slack may approve this action.", notify=True)
    kind = OwnerActionKind(action["kind"])
    approve, decline = _LABELS[kind]
    answer = row.answer_text.strip().casefold()
    if answer == decline:
        return _Decision("declined", "The owner declined.")
    if answer != approve:
        return _Decision("declined", "The answer did not match an approval option.", notify=True)
    try:
        return _Decision("carried_out", write=_PREPARE[kind](row, action, actor))
    except OwnerActionRefusedError as exc:
        return _Decision("refused", str(exc), notify=True)


def _carry_one(row: DeferredQuestion) -> _Decision | None:
    actor = _answered_by(row)
    decision = _decide(row, actor)
    with transaction.atomic():
        DeferredQuestion.objects.select_for_update().get(pk=row.pk)
        if DeferredQuestionAudit.objects.filter(question=row, action__in=_OUTCOMES).exists():
            return None
        note = decision.write() if decision.write is not None else decision.note
        DeferredQuestionAudit.objects.create(question=row, action=decision.outcome, resolver_id=actor, note=note)
    return decision


def _tell_owner(row: DeferredQuestion, reason: str) -> None:
    try:
        notify_user(
            f"Owner action for question {row.pk} did not change anything: {reason}",
            kind=NotifyKind.INFO,
            idempotency_key=f"owner-action-result:{row.pk}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.exception("Could not notify owner of refused action %s", row.pk)


def carry_out_owner_actions() -> list[str]:
    results: list[str] = []
    answered = (
        DeferredQuestion.objects.filter(
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            answered_at__isnull=False,
            evidence__has_key="action",
        )
        .exclude(audits__action__in=_OUTCOMES)
        .order_by("answered_at", "pk")
    )
    for row in answered:
        try:
            decision = _carry_one(row)
        except OwnerActionDeferredError as exc:
            logger.info("Owner action %s waits for live evidence: %s", row.pk, exc)
            continue
        if decision is None:
            continue
        results.append(f"{decision.outcome.replace('_', ' ')} {row.pk}")
        if decision.notify:
            _tell_owner(row, decision.note)
    return results
