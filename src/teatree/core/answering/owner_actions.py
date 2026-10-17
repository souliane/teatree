"""Carry out an owner's Slack answer: recheck the live evidence first, then write once under the lock."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone

from teatree.core.merge.void_merge import VoidRefusal, VoidRefusedError, approve_owner_void
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker, read_action
from teatree.core.modelkit.question_card import match_action_option
from teatree.core.models import Task, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.question_actions import ACTION_OUTCOMES
from teatree.core.models.task_handoff import release_checkout_park
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.worktree.checkout_attest import AttestRefusedError, CheckoutAttestation, read_attestation

logger = logging.getLogger(__name__)
_RETRY_AFTER = timedelta(minutes=5)
_EXPIRES_AFTER = timedelta(hours=24)

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

    def read() -> CheckoutAttestation:
        try:
            return read_attestation(
                worktree,
                expected_digest=action["binding"],
                requested_host_evidence=action.get("host_evidence"),
            )
        except AttestRefusedError as exc:
            message = f"The checkout {exc}"
            if exc.retryable:
                raise OwnerActionDeferredError(message) from exc
            raise OwnerActionRefusedError(message) from exc

    read()

    def write() -> str:
        attestation = read()
        attestation.record()
        for parked in Task.objects.filter(ticket=worktree.ticket, status=Task.Status.PENDING, not_before__isnull=False):
            latest = parked.attempts.order_by("-pk").values("result").first()
            result = latest["result"] if latest is not None else {}
            checkout_id = result.get("checkout_worktree_id") if isinstance(result, dict) else None
            if checkout_id == worktree.pk or (checkout_id is None and parked.pk == row.parked_task_id):
                release_checkout_park(parked)
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

    def write() -> str:
        try:
            return f"Voided merge record {approved.write().pk}."
        except VoidRefusedError as exc:
            raise OwnerActionRefusedError(exc.message) from exc

    return write


_PREPARE: dict[OwnerActionKind, Callable[[DeferredQuestion, ActionRequest, str], _Write]] = {
    OwnerActionKind.ATTEST: _prepare_attest,
    OwnerActionKind.VOID: _prepare_void,
}


def _resolver(answer: DeferredQuestionAudit | None) -> str:
    return answer.resolver_id if answer is not None else ""


def _decide(row: DeferredQuestion, answer: DeferredQuestionAudit | None) -> _Decision:
    action = read_action(row.evidence)
    if action is None or row.dedupe_marker != action_marker(action):
        return _Decision("refused", "The action card no longer matches its recorded request.", notify=True)
    if row.resolved_via != DeferredQuestion.ResolvedVia.SLACK or answer is None or answer.note != "button":
        return _Decision("refused", "Only the owner's Slack button tap may approve this action.", notify=True)
    kind = OwnerActionKind(action["kind"])
    option = match_action_option(row.options_json, row.answer_text)
    if option is None:
        return _Decision("refused", "The answer did not match a card option; ask again.", notify=True)
    if not option.recommended:
        return _Decision("declined", "The owner declined.")
    try:
        return _Decision("carried_out", write=_PREPARE[kind](row, action, answer.resolver_id))
    except OwnerActionRefusedError as exc:
        return _Decision("refused", str(exc), notify=True)


def _carry_one(
    row: DeferredQuestion, answer: DeferredQuestionAudit | None, *, forced: _Decision | None = None
) -> _Decision | None:
    decision = forced or _decide(row, answer)
    with transaction.atomic():
        DeferredQuestion.objects.select_for_update().get(pk=row.pk)
        if DeferredQuestionAudit.objects.filter(question=row, action__in=ACTION_OUTCOMES).exists():
            return None
        try:
            note = decision.write() if decision.write is not None else decision.note
        except OwnerActionRefusedError as exc:
            decision = _Decision("refused", str(exc), notify=True)
            note = decision.note
        DeferredQuestionAudit.objects.create(
            question=row, action=decision.outcome, resolver_id=_resolver(answer), note=note
        )
    return decision


def _defer_outcome(
    row: DeferredQuestion, answer: DeferredQuestionAudit | None, first_wait: datetime | None, reason: str
) -> _Decision | None:
    if first_wait is not None and timezone.now() - first_wait >= _EXPIRES_AFTER:
        expired = _Decision("refused", f"The action stayed unavailable for a day: {reason}", notify=True)
        return _carry_one(row, answer, forced=expired)
    with transaction.atomic():
        DeferredQuestion.objects.select_for_update().get(pk=row.pk)
        recent = DeferredQuestionAudit.objects.filter(question=row, action="deferred").order_by("-resolved_at").first()
        if DeferredQuestionAudit.objects.filter(question=row, action__in=ACTION_OUTCOMES).exists() or (
            recent is not None and timezone.now() - recent.resolved_at < _RETRY_AFTER
        ):
            return None
        DeferredQuestionAudit.objects.create(
            question=row, action="deferred", resolver_id=_resolver(answer), note=reason
        )
    if recent is None:
        _tell_owner(row, f"Waiting to complete the action: {reason}", event="waiting")
    logger.info("Owner action %s waits: %s", row.pk, reason)
    return None


def _carry_out(row: DeferredQuestion, first_wait: datetime | None) -> _Decision | None:
    answer = DeferredQuestionAudit.objects.owner_answer(row)
    try:
        return _carry_one(row, answer)
    except OwnerActionDeferredError as exc:
        return _defer_outcome(row, answer, first_wait, str(exc))


def _tell_owner(row: DeferredQuestion, reason: str, *, event: str = "outcome") -> None:
    try:
        notify_user(
            f"{row.question} Nothing changed: {reason}",
            kind=NotifyKind.INFO,
            idempotency_key=f"owner-action-result:{row.pk}:{event}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.exception("Could not notify owner of refused action %s", row.pk)


def carry_out_owner_actions() -> list[str]:
    results: list[str] = []
    for row in DeferredQuestion.objects.awaiting_action_outcome():
        waited = DeferredQuestionAudit.objects.filter(question=row, action="deferred").aggregate(
            first=Min("resolved_at"), last=Max("resolved_at")
        )
        if waited["last"] is not None and timezone.now() - waited["last"] < _RETRY_AFTER:
            continue
        try:
            decision = _carry_out(row, waited["first"])
        except Exception:
            logger.exception("Owner action %s failed; retrying after a delay", row.pk)
            try:
                decision = _defer_outcome(
                    row,
                    DeferredQuestionAudit.objects.owner_answer(row),
                    waited["first"],
                    "The action could not complete.",
                )
            except Exception:
                logger.exception("Owner action %s could not record its failed attempt", row.pk)
                continue
        if decision is None:
            continue
        results.append(f"{decision.outcome.replace('_', ' ')} {row.pk}")
        if decision.notify:
            _tell_owner(row, decision.note)
    return results
