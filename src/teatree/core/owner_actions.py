"""Carry out an owner's Slack answer after rechecking the action's live evidence."""

import logging

from django.db import transaction

from teatree.core.merge.void_merge import VoidRefusal, VoidRefusedError, apply_owner_approved_void
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker, read_action
from teatree.core.models import Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.task_handoff import release_checkout_park
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.worktree.checkout_attest import AttestRefusedError, attest_checkout

logger = logging.getLogger(__name__)
_OUTCOMES = ("carried_out", "declined", "refused")


class OwnerActionDeferredError(Exception):
    pass


class OwnerActionRefusedError(Exception):
    pass


def _attest(row: DeferredQuestion, action: ActionRequest) -> str:
    try:
        worktree_id = int(action["target"])
    except ValueError as exc:
        message = "The checkout reference is invalid."
        raise OwnerActionRefusedError(message) from exc
    worktree = Worktree.objects.filter(pk=worktree_id).first()
    if worktree is None:
        message = "The checkout no longer has a record."
        raise OwnerActionRefusedError(message)
    try:
        attestation = attest_checkout(worktree, expected_digest=action["binding"])
    except AttestRefusedError as exc:
        if "live or unverifiable processes" in str(exc) or "unoccupied" in str(exc):
            raise OwnerActionDeferredError(str(exc)) from exc
        raise OwnerActionRefusedError(str(exc)) from exc
    if row.parked_task is not None:
        release_checkout_park(row.parked_task)
    return f"Attested the checkout at {attestation.state.digest}."


def _void(action: ActionRequest, answered_by: str) -> str:
    try:
        void = apply_owner_approved_void(
            action["target"], binding=action["binding"], reason=action.get("note", ""), actor=answered_by
        )
    except VoidRefusedError as exc:
        if exc.code == VoidRefusal.FORGE_UNREADABLE:
            raise OwnerActionDeferredError(exc.message) from exc
        raise OwnerActionRefusedError(exc.message) from exc
    return f"Voided merge record {void.pk}."


def _record(row: DeferredQuestion, outcome: str, actor: str, note: str) -> str:
    DeferredQuestionAudit.objects.create(question=row, action=outcome, resolver_id=actor, note=note)
    return f"{'carried out' if outcome == 'carried_out' else outcome} {row.pk}"


def _carry_one(question_id: int) -> tuple[str | None, str]:
    with transaction.atomic():
        row = DeferredQuestion.objects.select_for_update().get(pk=question_id)
        if DeferredQuestionAudit.objects.filter(question=row, action__in=_OUTCOMES).exists():
            return None, ""
        action = read_action(row.evidence)
        answer = (
            DeferredQuestionAudit.objects.filter(question=row, action="answered", resolver_id__startswith="slack:")
            .order_by("-pk")
            .first()
        )
        actor = answer.resolver_id if answer is not None else ""
        if action is None or row.dedupe_marker != action_marker(action):
            note = "The action card no longer matches its recorded request."
            return _record(row, "refused", actor, note), note
        if row.resolved_via != DeferredQuestion.ResolvedVia.SLACK or not actor:
            note = "Only an authenticated owner answer in Slack may approve this action."
            return _record(row, "refused", actor, note), note
        labels = {
            OwnerActionKind.ATTEST: ("accept", "decline"),
            OwnerActionKind.VOID: ("void", "keep"),
        }
        approve, decline = labels[OwnerActionKind(action["kind"])]
        answer_text = row.answer_text.strip().casefold()
        if answer_text != approve:
            note = "The owner declined." if answer_text == decline else "The answer did not match an approval option."
            return _record(row, "declined", actor, note), note if answer_text != decline else ""
        try:
            note = _attest(row, action) if action["kind"] == OwnerActionKind.ATTEST else _void(action, actor)
        except OwnerActionRefusedError as exc:
            note = str(exc)
            return _record(row, "refused", actor, note), note
        return _record(row, "carried_out", actor, note), ""


def carry_out_owner_actions() -> list[str]:
    results: list[str] = []
    candidates = DeferredQuestion.objects.filter(
        audience=DeferredQuestion.Audience.OWNER_QUESTION,
        answered_at__isnull=False,
        evidence__has_key="action",
    ).order_by("answered_at", "pk")
    for row in candidates:
        try:
            result, notice = _carry_one(row.pk)
        except OwnerActionDeferredError:
            logger.info("Owner action %s waits for live evidence", row.pk)
            continue
        if result is None:
            continue
        results.append(result)
        if notice:
            try:
                notify_user(
                    f"Owner action for question {row.pk} did not change anything: {notice}",
                    kind=NotifyKind.INFO,
                    idempotency_key=f"owner-action-result:{row.pk}",
                    audience=NotifyAudience.OWNER_ESCALATION,
                )
            except Exception:
                logger.exception("Could not notify owner of refused action %s", row.pk)
    return results
