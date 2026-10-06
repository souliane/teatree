"""Send the review request the triage ladder finds owed.

:class:`~teatree.loop.scanners.mr_triage_scan.MrTriageScanner` decides; this acts on its
``REQUEST_REVIEW`` verdicts through the sanctioned ``review_request_post`` command, so the
dedup claim, the posture gate, the post and the ``ReviewRequestPost`` record stay in that
one chokepoint. Two selections are this caller's own, because the command trusts a human
to have made them: a ticket to read the anti-vacuity attestation from, and a ``merge_safe``
cold review bound to the CURRENT head — the command's review-state gate is not head-bound.

A refusal the owner can fix becomes their question; any other refusal clears on its own and
is retried on the next pass, the command having rolled its dedup claim back.
"""

import io
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol

from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.review.mr_state_question import ask_mr_state, retire_mr_state_question
from teatree.core.review.mr_triage import TriageAction
from teatree.loop.scanners.base import ScanSignal, SignalPayload
from teatree.loop.scanners.mr_triage_scan import MISSING_REVIEW_OPTIONS, MrTriageScanner, TriagedMr
from teatree.loop.scanners.my_prs import _str_field
from teatree.loop.scanners.pr_payload import head_sha
from teatree.loop.scanners.pr_sweep_decision import has_independent_cold_review
from teatree.types import RawAPIDict
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

_OWNER_ACTIONABLE = frozenset(
    {"no_ticket", "anti_vacuity_not_attested", "ticket_not_reviewed", "on_behalf_not_approved"}
)


class ReviewRequestPoster(Protocol):
    def post(self, *, mr_url: str, title: str, ticket_id: str, head_sha: str) -> RawAPIDict: ...


@dataclass(frozen=True, slots=True)
class CallCommandReviewRequestPoster:
    """The ``review_request_post`` command; its printed JSON verdict is the result."""

    approver: str = "review_request_send"

    def post(self, *, mr_url: str, title: str, ticket_id: str, head_sha: str) -> RawAPIDict:
        from teatree.core.machine_output import (  # noqa: PLC0415 — deferred: the command registry needs Django
            call_command_streamed,
            last_json_object,
        )

        out = io.StringIO()
        with suppress(SystemExit):
            call_command_streamed(
                "review_request_post",
                *("--mr-url", mr_url, "--approver", self.approver, "--title", title),
                *("--ticket-id", ticket_id, "--head-sha", head_sha),
                stream=out,
            )
        verdict = last_json_object(out.getvalue())
        if verdict is None:
            logger.warning(
                "review_request_send: no verdict from review_request_post for %s: %s", mr_url, out.getvalue()
            )
            return {"action": "refused", "reason": "unreadable_result"}
        return verdict


@dataclass(frozen=True, slots=True)
class _Sendable:
    ticket_id: str
    head_sha: str


@dataclass(slots=True)
class ReviewRequestSendScanner:
    """Send each owed review request, invoking the command at most ``max_posts_per_tick`` times a pass."""

    triage: MrTriageScanner
    poster: ReviewRequestPoster = field(default_factory=CallCommandReviewRequestPoster)
    max_posts_per_tick: int = 3
    name: str = "review_request_send"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        posts = 0
        for item in self.triage.triaged():
            if item.verdict.action is not TriageAction.REQUEST_REVIEW:
                continue
            sendable = self._sendable(item)
            if isinstance(sendable, ScanSignal):
                signals.append(sendable)
                continue
            if posts == self.max_posts_per_tick:
                break
            posts += 1
            result = self.poster.post(
                mr_url=item.url,
                title=_str_field(item.pr, "title"),
                ticket_id=sendable.ticket_id,
                head_sha=sendable.head_sha,
            )
            signals.append(self._outcome(item, result))
        return signals

    @staticmethod
    def _sendable(item: TriagedMr) -> _Sendable | ScanSignal:
        ref = pr_ref_from_url(item.url)
        sha = head_sha(item.pr)
        if ref is None or not sha:
            return _deferred(item, "head_unreadable")
        ticket = resolve_gated_ticket(slug=ref.slug, pr_id=ref.pr_id)
        if ticket is None:
            return _refused(item, "no_ticket")
        if not has_independent_cold_review(slug=ref.slug, pr_id=ref.pr_id, head_sha=sha):
            ask_mr_state(
                mr_url=item.url,
                reason="it is ready for review, but no independent cold review covers its current head yet.",
                options=MISSING_REVIEW_OPTIONS,
            )
            return _deferred(item, "awaiting_cold_review")
        return _Sendable(ticket_id=str(ticket.pk), head_sha=sha)

    @staticmethod
    def _outcome(item: TriagedMr, result: RawAPIDict) -> ScanSignal:
        action = str(result.get("action") or "")
        reason = str(result.get("reason") or "")
        if action == "post":
            retire_mr_state_question(item.url, reason="the review request was sent")
            return _sent(item, str(result.get("permalink") or ""))
        if action == "refused" and reason in _OWNER_ACTIONABLE:
            return _refused(item, reason)
        return _deferred(item, f"{action}:{reason}")


def _sent(item: TriagedMr, permalink: str) -> ScanSignal:
    return ScanSignal(
        kind="review_request.sent",
        summary=_summary(item, "sent", permalink),
        payload=_payload(item, permalink=permalink),
    )


def _refused(item: TriagedMr, reason: str) -> ScanSignal:
    ask_mr_state(
        mr_url=item.url,
        reason=f"it is ready for review, but sending the review request was refused ({reason}).",
        options=MISSING_REVIEW_OPTIONS,
    )
    return ScanSignal(
        kind="review_request.send_refused",
        summary=_summary(item, "refused", reason),
        payload=_payload(item, reason=reason),
    )


def _deferred(item: TriagedMr, reason: str) -> ScanSignal:
    return ScanSignal(
        kind="review_request.send_deferred",
        summary=_summary(item, "deferred", reason),
        payload=_payload(item, reason=reason),
    )


def _summary(item: TriagedMr, verdict: str, detail: str) -> str:
    return f"{item.url} review request {verdict} ({detail}): {_str_field(item.pr, 'title')}"


def _payload(item: TriagedMr, *, reason: str = "", permalink: str = "") -> SignalPayload:
    return {"url": item.url, "title": _str_field(item.pr, "title"), "reason": reason, "permalink": permalink}


__all__ = ["CallCommandReviewRequestPoster", "ReviewRequestPoster", "ReviewRequestSendScanner"]
