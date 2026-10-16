"""Carry out an owner's Slack answer: recheck the live evidence first, then write once under the lock."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from teatree.core.merge.void_merge import VoidRefusal, VoidRefusedError, approve_owner_void
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker, read_action
from teatree.core.modelkit.question_card import match_action_option
from teatree.core.models import Task, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.question_actions import ACTION_OUTCOMES, action_awaiting_outcome, owner_answerer
from teatree.core.models.task_handoff import release_checkout_park
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.worktree.checkout_attest import AttestRefusedError, read_attestation

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


def _prepare_attest(_row: DeferredQuestion, action: ActionRequest, _actor: str) -> _Write:
    worktree = Worktree.objects.filter(pk=action["target"]).first() if action["target"].isdigit() else None
    if worktree is None:
        message = "The checkout no longer has a record."
        raise OwnerActionRefusedError(message)
    try:
        attestation = read_attestation(
            worktree,
            expected_digest=action["binding"],
            requested_host_evidence=action.get("host_evidence"),
        )
    except AttestRefusedError as exc:
        message = f"The checkout {exc}"
        if exc.retryable:
            raise OwnerActionDeferredError(message) from exc
        raise OwnerActionRefusedError(message) from exc

    def write() -> str:
        attestation.record()
        for parked in Task.objects.filter(ticket=worktree.ticket, status=Task.Status.PENDING, not_before__isnull=False):
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
    return lambda: f"Voided merge record {approved.write().pk}."


_PREPARE: dict[OwnerActionKind, Callable[[DeferredQuestion, ActionRequest, str], _Write]] = {
    OwnerActionKind.ATTEST: _prepare_attest,
    OwnerActionKind.VOID: _prepare_void,
}


def _decide(row: DeferredQuestion, actor: str) -> _Decision:
    action = read_action(row.evidence)
    if action is None or row.dedupe_marker != action_marker(action):
        return _Decision("refused", "The action card no longer matches its recorded request.", notify=True)
    proof = (
        DeferredQuestionAudit.objects.filter(question=row, action="answered", resolver_id=actor).order_by("-pk").first()
    )
    if (
        row.resolved_via != DeferredQuestion.ResolvedVia.SLACK
        or not actor
        or proof is None
        or (proof.note != "button" and not proof.note.startswith("verified-typed:"))
    ):
        return _Decision("refused", "Only the owner's verified Slack answer may approve this action.", notify=True)
    kind = OwnerActionKind(action["kind"])
    option = match_action_option(row.options_json, row.answer_text)
    if option is None:
        return _Decision("refused", "The answer did not match a card option; ask again.", notify=True)
    if not option.recommended:
        return _Decision("declined", "The owner declined.")
    try:
        return _Decision("carried_out", write=_PREPARE[kind](row, action, actor))
    except OwnerActionRefusedError as exc:
        return _Decision("refused", str(exc), notify=True)


def _carry_one(row: DeferredQuestion, *, forced: _Decision | None = None) -> _Decision | None:
    actor = owner_answerer(row, DeferredQuestionAudit)
    decision = forced or _decide(row, actor)
    with transaction.atomic():
        DeferredQuestion.objects.select_for_update().get(pk=row.pk)
        if DeferredQuestionAudit.objects.filter(question=row, action__in=ACTION_OUTCOMES).exists():
            return None
        note = decision.write() if decision.write is not None else decision.note
        DeferredQuestionAudit.objects.create(question=row, action=decision.outcome, resolver_id=actor, note=note)
    return decision


def _tell_owner(row: DeferredQuestion, reason: str, *, event: str = "outcome") -> None:
    try:
        notify_user(
            f"Owner action for question {row.pk} did not change anything: {reason}",
            kind=NotifyKind.INFO,
            idempotency_key=f"owner-action-result:{row.pk}:{event}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.exception("Could not notify owner of refused action %s", row.pk)


def carry_out_owner_actions() -> list[str]:
    results: list[str] = []
    for row in action_awaiting_outcome(DeferredQuestion):
        last_defer = (
            DeferredQuestionAudit.objects.filter(question=row, action="deferred").order_by("-resolved_at").first()
        )
        expired = row.answered_at is not None and timezone.now() - row.answered_at >= _EXPIRES_AFTER
        if not expired and last_defer is not None and timezone.now() - last_defer.resolved_at < _RETRY_AFTER:
            continue
        try:
            forced = _Decision("refused", "Live evidence remained unavailable for 24 hours; ask again.", notify=True)
            decision = _carry_one(row, forced=forced if expired else None)
        except OwnerActionDeferredError as exc:
            DeferredQuestionAudit.objects.create(
                question=row, action="deferred", resolver_id=owner_answerer(row, DeferredQuestionAudit), note=str(exc)
            )
            if last_defer is None:
                _tell_owner(row, f"Waiting for live evidence: {exc}", event="waiting")
            logger.info("Owner action %s waits for live evidence: %s", row.pk, exc)
            continue
        except Exception:
            logger.exception("Owner action %s failed; retrying next pass", row.pk)
            continue
        if decision is None:
            continue
        results.append(f"{decision.outcome.replace('_', ' ')} {row.pk}")
        if decision.notify:
            _tell_owner(row, decision.note)
    return results
