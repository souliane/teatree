"""Send the review request the triage ladder finds owed.

:class:`~teatree.loop.scanners.mr_triage_scan.MrTriageScanner` decides; this acts on its
``REQUEST_REVIEW`` verdicts through the sanctioned ``review_request_post`` command, so the
dedup claim, the posture gate, the send-proxy, the post and the ``ReviewRequestPost`` record
stay in that one chokepoint. What the command trusts a human to have checked is this caller's
own: the owner's answer at the CURRENT head, no standing hold, a ``merge_safe`` cold review
bound to that head (or the owner's "post" answer in its place), a ticket to read the
anti-vacuity attestation from, a PR title and body the overlay accepts, and a live head that
still matches the listing.

A refusal asks the owner once per head, and an answer at a head is final for that head. Only
the outcomes in :data:`_RETRIED_QUIETLY` retry on their own; every other one, including a
reason added later, is put to the owner. A pass stops after ``max_posts_per_tick`` landed
posts, and one merge request raising never ends the pass for the rest.
"""

import io
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol

from teatree.core.machine_output import call_command_streamed, last_json_object
from teatree.core.merge.ticket_resolution import resolve_gated_ticket
from teatree.core.modelkit.forge_readability import HEAD_SHA_UNREADABLE
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import DeferredQuestion
from teatree.core.overlay_metadata import OverlayMetadata
from teatree.core.review.mr_state_question import (
    OwnerAsk,
    ask_mr_state,
    owner_answer_at_head,
    retire_head_bound_question,
    retire_mr_state_question,
)
from teatree.core.review.mr_triage import TriageAction
from teatree.loop.scanners.base import ScanSignal, SignalPayload
from teatree.loop.scanners.mr_triage_scan import MISSING_REVIEW_OPTIONS, MrTriageScanner, TriagedMr
from teatree.loop.scanners.my_prs import _str_field
from teatree.loop.scanners.pr_payload import head_sha
from teatree.loop.scanners.pr_sweep_decision import head_review_state
from teatree.types import RawAPIDict
from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

_SENT = "review_request.sent"
_POST_ANSWER = MISSING_REVIEW_OPTIONS[0].casefold()
_RETRIED_QUIETLY = frozenset(
    {
        "read_failed_failsafe",
        "already_claimed",
        "already_posted",
        "authorship_unreadable",
        "draft_state_unknown",
    }
)
_ASK_TEXT = {"awaiting_cold_review": "no independent cold review covers its current head yet."}


class ReviewRequestPoster(Protocol):
    def post(self, *, mr_url: str, title: str, ticket_id: str, head_sha: str) -> RawAPIDict: ...


@dataclass(frozen=True, slots=True)
class CallCommandReviewRequestPoster:
    """The ``review_request_post`` command; its printed JSON verdict is the result."""

    approver: str = "review_request_send"

    def post(self, *, mr_url: str, title: str, ticket_id: str, head_sha: str) -> RawAPIDict:
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


@dataclass(slots=True)
class ReviewRequestSendScanner:
    """Send each owed review request, stopping once ``max_posts_per_tick`` have landed this pass."""

    triage: MrTriageScanner
    poster: ReviewRequestPoster = field(default_factory=CallCommandReviewRequestPoster)
    metadata: OverlayMetadata = field(default_factory=OverlayMetadata)
    max_posts_per_tick: int = 3
    name: str = "review_request_send"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        posts = 0
        for item in self.triage.triaged():
            if item.verdict.action is not TriageAction.REQUEST_REVIEW:
                continue
            if posts == self.max_posts_per_tick:
                break
            try:
                signal = self._act(item)
            except Exception as exc:
                logger.exception("review_request_send: sending the review request for %s raised", item.url)
                signal = _deferred(item, f"error:{type(exc).__name__}")
            if signal.kind == _SENT:
                posts += 1
            signals.append(signal)
        return signals

    def _act(self, item: TriagedMr) -> ScanSignal:
        ref = pr_ref_from_url(item.url)
        sha = head_sha(item.pr)
        if ref is None or not sha:
            return _deferred(item, "head_unreadable")
        answer = owner_answer_at_head(item.url, head_sha=sha)
        if blocker := _review_blocker(item, ref, sha, answer):
            return blocker
        return self._send(item, ref, sha)

    def _send(self, item: TriagedMr, ref: PrRef, sha: str) -> ScanSignal:
        ticket = resolve_gated_ticket(slug=ref.slug, pr_id=ref.pr_id)
        if ticket is None:
            return _ask(item, sha, "no_ticket")
        title = _str_field(item.pr, "title")
        if self.metadata.validate_pr(title, _str_field(item.pr, "description", "body"), repo=ref.slug)["errors"]:
            return _ask(item, sha, "pr_metadata_invalid")
        if moved := self._live_head_mismatch(ref, sha):
            return _deferred(item, moved)
        result = self.poster.post(mr_url=item.url, title=title, ticket_id=str(ticket.pk), head_sha=sha)
        return _outcome(item, sha, result)

    def _live_head_mismatch(self, ref: PrRef, sha: str) -> str:
        live = self.triage.host.fetch_live_head_sha(slug=ref.slug, pr_id=ref.pr_id)
        if live in {"", HEAD_SHA_UNREADABLE}:
            return "head_unreadable"
        return "" if live == sha else "head_moved"


def _review_blocker(item: TriagedMr, ref: PrRef, sha: str, answer: DeferredQuestion | None) -> ScanSignal | None:
    """What stops a send at this head: the owner declining, a standing hold, or no review and no "post" answer."""
    if answer is not None and answer.answer_text.strip().casefold() != _POST_ANSWER:
        return _deferred(item, "owner_declined")
    review = head_review_state(slug=ref.slug, pr_id=ref.pr_id, head_sha=sha)
    if review.held_verdicts:
        retire_head_bound_question(item.url, reason="a cold review holds this head")
        return _deferred(item, review.hold_reason, detail=review.hold_detail)
    if review.authorizing_verdict is None and answer is None:
        return _ask(item, sha, "awaiting_cold_review")
    return None


def _outcome(item: TriagedMr, sha: str, result: RawAPIDict) -> ScanSignal:
    action = str(result.get("action") or "")
    reason = str(result.get("reason") or "")
    if action == "post":
        retire_mr_state_question(item.url, reason="the review request was sent")
        return _sent(item, str(result.get("permalink") or ""))
    if reason in _RETRIED_QUIETLY:
        return _deferred(item, f"{action}:{reason}")
    return _ask(item, sha, reason or action or "no_verdict")


def _ask(item: TriagedMr, sha: str, reason: str) -> ScanSignal:
    """Refuse *item* and put it to the owner; a head they already answered about is not asked again."""
    text = _ASK_TEXT.get(reason, f"sending the review request was refused ({reason}).")
    question = ask_mr_state(
        mr_url=item.url,
        reason=f"it is ready for review, but {text}",
        options=MISSING_REVIEW_OPTIONS,
        head_sha=sha,
        owner=OwnerAsk(
            OwnerDecision.PUBLIC_POST,
            [f"review request refused: {reason}", f"head {sha[:12]}", f"triage verdict: {item.verdict.action}"],
        ),
    )
    asked = question is not None
    return ScanSignal(
        kind="review_request.send_refused",
        summary=_summary(item, "refused", f"{reason}; owner {'asked' if asked else 'not asked'}"),
        payload={**_payload(item, reason=reason), "asked": asked},
    )


def _sent(item: TriagedMr, permalink: str) -> ScanSignal:
    return ScanSignal(
        kind=_SENT, summary=_summary(item, "sent", permalink), payload=_payload(item, permalink=permalink)
    )


def _deferred(item: TriagedMr, reason: str, *, detail: str = "") -> ScanSignal:
    return ScanSignal(
        kind="review_request.send_deferred",
        summary=_summary(item, "deferred", f"{reason}: {detail}" if detail else reason),
        payload={**_payload(item, reason=reason), "detail": detail},
    )


def _summary(item: TriagedMr, verdict: str, detail: str) -> str:
    return f"{item.url} review request {verdict} ({detail}): {_str_field(item.pr, 'title')}"


def _payload(item: TriagedMr, *, reason: str = "", permalink: str = "") -> SignalPayload:
    return {"url": item.url, "title": _str_field(item.pr, "title"), "reason": reason, "permalink": permalink}


__all__ = ["CallCommandReviewRequestPoster", "ReviewRequestPoster", "ReviewRequestSendScanner"]
