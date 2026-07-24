"""Server-side persistence for the shell-denied reactive phases' typed envelopes (#9).

The reactive phases (``scanning_news`` / ``triage_assessing`` / ``answering``)
run shell-denied: their headless agent cannot run the ``t3`` CLI, so it hands its
work back through a typed result-envelope channel and THIS module is the
server-side half that persists the returned structure. Split out of
``attempt_recorder`` (at its module-health LOC cap) — ``record_result_envelope``
calls :func:`record_reactive_envelopes` after the evidence gate has already
refused a summary-only run, so each channel field is present and non-empty here.
"""

import logging
from typing import cast

from django.utils import timezone

from teatree.agents.result_schema import (
    AgentResultBlob,
    AnswerEnvelope,
    ArticleSuggestion,
    TriageRecommendation,
    answer_text,
    recommendation_issue_url,
    suggestion_url,
)
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import DeferredQuestion, PendingArticleSuggestion, PendingTriageRecommendation, Task
from teatree.core.news_digest import DigestItem, render_digest
from teatree.core.notify import NotifyKind, notify_user

#: Shell-denied reactive phases whose headless agent hands its work back through
#: a typed envelope channel (#9): the agent cannot run the ``t3`` CLI, so the
#: recorder is the server-side half that persists the returned structure. The
#: ``PHASE_REQUIRED_EVIDENCE`` gate has already refused a summary-only run before
#: these fire, so the channel field is present and non-empty here.
_SCANNING_NEWS_PHASE = "scanning_news"
_TRIAGE_ASSESSING_PHASE = "triage_assessing"
_ANSWERING_PHASE = "answering"

logger = logging.getLogger(__name__)


def record_reactive_envelopes(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    """Persist whichever reactive-phase envelope channel *task*'s phase owns.

    Each helper self-gates on the phase, so this dispatches to all three and the
    non-matching ones are no-ops — one call site in ``record_result_envelope``.
    """
    _maybe_record_article_suggestions(task, result, phase=phase)
    _maybe_record_triage_recommendations(task, result, phase=phase)
    _maybe_record_answer_draft(task, result, phase=phase)


def _maybe_record_article_suggestions(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    """Persist a scanning_news agent's returned ``article_suggestions`` (corr-11, #9).

    One ``PENDING`` :class:`PendingArticleSuggestion` per candidate, idempotent by
    source URL (a re-scan never duplicates) and behind the same ask-gate the
    scanner used to enqueue directly — the shell-denied agent hands the batch
    back, the server persists it. After at least one NEW candidate is recorded,
    ONE :class:`DeferredQuestion` DMs the user the batch for approval, deduped per
    task so a re-run never re-asks — the approval surface the shell-denied agent
    cannot post itself. Unlike the triage batch it carries no ``parked_task``: an
    approval reply records the decision, it must NOT re-queue a headless re-scan
    of a shell-denied phase that cannot file the issue anyway. A non-scanning_news
    phase or a result with no ``article_suggestions`` list is a no-op.
    """
    if normalize_phase(phase or task.phase) != _SCANNING_NEWS_PHASE:
        return
    suggestions = result.get("article_suggestions")
    if not isinstance(suggestions, list):
        return
    overlay = task.ticket.overlay
    recorded: list[PendingArticleSuggestion] = []
    for raw_item in suggestions:
        url = suggestion_url(raw_item)
        if not url:
            continue
        item = cast("ArticleSuggestion", raw_item)
        row = PendingArticleSuggestion.record_candidate(
            url=url,
            title=str(item.get("title") or ""),
            summary=str(item.get("rationale") or ""),
            overlay=overlay,
        )
        if row is not None:
            recorded.append(row)
    if not recorded:
        return
    DeferredQuestion.record(
        question=_article_batch_question(recorded),
        session_id=task.claimed_by_session or "",
        dedupe_marker=f"news-batch-{task.pk}",
    )
    _post_press_review_digest(task, recorded)


def _post_press_review_digest(task: Task, rows: "list[PendingArticleSuggestion]") -> None:
    """DM the owner the Slack-formatted press review for this scan (#3669).

    The digest is the DELIVERY the merged press-review sources feed; the
    ``DeferredQuestion`` above remains the ask-gate record. Both exist because
    they answer different questions — the question is the approval surface, the
    digest is the read. Deduped per task by ``notify_user``'s idempotency key, so
    a re-recorded envelope never re-posts.

    Record-and-proceed: a Slack failure is logged and swallowed. The candidates
    are already persisted, and losing the read must never lose the backlog.
    """
    text = render_digest(
        [DigestItem(title=row.title or row.url, url=row.url, rationale=row.summary) for row in rows],
        scanned_on=timezone.localdate().isoformat(),
    )
    if not text:
        return
    try:
        notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key=f"news-digest-{task.pk}",
            audience=NotifyAudience.OWNER_DELIVERY,
            linkify=False,
        )
    except Exception:
        logger.warning("press-review digest post failed — candidates are recorded regardless", exc_info=True)


def _article_batch_question(rows: "list[PendingArticleSuggestion]") -> str:
    """The batch-approval DM listing each queued news candidate, one per line."""
    lines = [
        (
            f"Scanned AI news: {len(rows)} new candidate(s) queued behind the ask-gate. "
            "Nothing is filed as an issue until you approve — review and approve/reject each:"
        ),
    ]
    lines.extend(f"• {row.title or row.url} — {row.url}" for row in rows)
    return "\n".join(lines)


def _maybe_record_triage_recommendations(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    """Persist a triage_assessing agent's returned ``triage_recommendations`` (corr-11, #9).

    One ``PENDING`` :class:`PendingTriageRecommendation` per assessed issue, idempotent
    by issue URL (a re-assessment never duplicates) and fail-closed on an unknown
    verdict — the shell-denied assessor hands the batch back and the server persists
    it. After at least one row is recorded, ONE
    :class:`DeferredQuestion` DMs the user the batch summary (correlated to the task
    via ``parked_task``, deduped per task so a resume never re-asks). **Nothing acts
    autonomously**: the interactive ``t3:triaging-issues`` skill approves/acts. A
    non-triage_assessing phase or a result with no ``triage_recommendations`` list is
    a no-op.
    """
    if normalize_phase(phase or task.phase) != _TRIAGE_ASSESSING_PHASE:
        return
    recommendations = result.get("triage_recommendations")
    if not isinstance(recommendations, list):
        return
    overlay = task.ticket.overlay
    recorded = 0
    for raw_item in recommendations:
        issue_url = recommendation_issue_url(raw_item)
        if not issue_url:
            continue
        item = cast("TriageRecommendation", raw_item)
        raw_labels = item.get("suggested_labels")
        labels = [s for s in raw_labels if isinstance(s, str)] if isinstance(raw_labels, list) else []
        row = PendingTriageRecommendation.record_candidate(
            issue_url=issue_url,
            verdict=str(item.get("verdict") or ""),
            title=str(item.get("title") or ""),
            suggested_labels=labels,
            priority=str(item.get("priority") or ""),
            duplicate_of=str(item.get("duplicate_of") or ""),
            rationale=str(item.get("rationale") or ""),
            overlay=overlay,
        )
        if row is not None:
            recorded += 1
    if recorded == 0:
        return
    DeferredQuestion.record(
        question=(
            f"Triaged {recorded} open needs-triage issue(s). Review and approve/reject each "
            f"recommendation with /t3:triaging-issues — nothing is acted on until you approve."
        ),
        session_id=task.claimed_by_session or "",
        parked_task=task,
        dedupe_marker=f"triage-batch-{task.pk}",
    )


def _maybe_record_answer_draft(task: Task, result: AgentResultBlob, *, phase: str) -> None:
    """Deliver an answering agent's returned ``answer`` draft (corr-11, #9).

    A reply to the OWNER's own inbound DM (an answering task the reactive
    Slack-answer cycle delegated, carrying its ``slack_answer`` context on the
    ticket) is SENT immediately, threaded under the owner's message — answering
    the owner is never a post *on the owner's behalf*, so it must never route
    through the away/approval defer gate that parks the box's self-initiated
    questions. Deferring the owner's own reply is exactly the bug where an owner
    DM in ``autonomous_away`` got a "Approve this drafted reply?" pending
    question parked instead of an answer.

    Any other answering task (a colleague/channel thread the agent answers on
    the user's behalf) still hands the draft back through a
    :class:`DeferredQuestion` (correlated via ``parked_task``) for approval —
    that on-behalf gate is unchanged. A non-answering phase or a result with no
    ``answer`` text is a no-op.
    """
    if normalize_phase(phase or task.phase) != _ANSWERING_PHASE:
        return
    raw_answer = result.get("answer")
    text = answer_text(raw_answer)
    if not text:
        return
    if _post_owner_dm_reply(task, text):
        return
    answer = cast("AnswerEnvelope", raw_answer)
    thread_ref = str(answer.get("thread_ref") or "").strip()
    where = f" (thread {thread_ref})" if thread_ref else ""
    DeferredQuestion.record(
        question=f"Approve this drafted reply{where}?\n\n{text}",
        session_id=task.claimed_by_session or "",
        parked_task=task,
    )


def _post_owner_dm_reply(task: Task, text: str) -> bool:
    """Send *text* as a threaded reply to the owner's inbound DM; ``True`` if sent.

    The reactive Slack-answer cycle stamps the authoritative Slack coordinates
    onto the delegated ticket as ``extra["slack_answer"]`` — ``channel`` and the
    owner-message ``slack_ts``. Posting with ``ts=slack_ts`` threads the reply
    under the owner's own message (a top-level DM roots on its own ts), so the
    answer lands in the owner's thread, not as a new root or under a stale DM
    cache. The ticket's ``slack_ts`` is the authoritative thread target — the
    agent-returned ``thread_ref`` is advisory and may be blank or wrong.

    Returns ``False`` (so the caller falls back to the on-behalf approval path,
    losing nothing) when the ticket carries no ``slack_answer`` context, the
    coordinates are incomplete, no messaging backend resolves, or the post does
    not confirm ``ok``. On a confirmed send the owner-question row is stamped
    ``answered_at`` so the #1063 turn-end gate stops nagging for it.
    """
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415 — deferred: call-time import
    from teatree.core.models import PendingChatInjection  # noqa: PLC0415 — deferred: call-time import

    slack_answer = (task.ticket.extra or {}).get("slack_answer")
    if not isinstance(slack_answer, dict):
        return False
    channel = str(slack_answer.get("channel") or "").strip()
    slack_ts = str(slack_answer.get("slack_ts") or "").strip()
    if not channel or not slack_ts:
        return False
    backend = messaging_from_overlay(task.ticket.overlay or None)
    if backend is None:
        return False
    try:
        resp = backend.post_reply(channel=channel, ts=slack_ts, text=text)
    except Exception:  # noqa: BLE001 — a Slack failure falls back to the approval path, never drops the reply
        return False
    if not isinstance(resp, dict) or resp.get("ok") is not True:
        return False
    PendingChatInjection.agent_answered_question(slack_ts)
    return True
