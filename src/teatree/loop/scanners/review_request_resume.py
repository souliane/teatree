"""R3 — a paused review request resumes IN ITS OWN THREAD, never as a second broadcast.

The owner holds a review request by reacting to its Slack message with a pause
emoji: "more to fix, not reviewable yet". When the merge request later becomes
genuinely ready, a fresh top-level post would split the discussion in two — the
reviewers who already saw the request now hold two messages for one merge
request, and the thread carrying the context is the one nobody is reading. So
the resume is a short reply INTO the existing thread, on the ``(channel,
thread_ts)`` coordinates the ``ReviewRequestPost`` row already holds.

``resumed_at`` is the single-use stamp, claimed with a conditional ``UPDATE ...
WHERE resumed_at IS NULL`` *before* the post: two concurrent ticks read the same
open row, so a claim taken afterwards would let both reply. The tick that loses
the claim matches zero rows and stands down. A gated or failed post RELEASES the
claim, because a one-shot consumed by a missing approval or a Slack outage is a
resume that never happens.

An UNREADABLE pause is surfaced rather than skipped in silence. To every caller
downstream a failed read looks exactly like a request nobody held, so a scanner
that treats it as "not paused" reports a healthy queue while the resume it owes
is structurally unreachable — the same laundering
:mod:`teatree.core.review.review_pause` refuses to do at the reader.

Readiness is the forge's own answer on both axes and both fail CLOSED: only a
code-host-CONFIRMED non-draft merge request whose branch-protection-required
checks are live-green resumes. An unparsable URL, an unreadable draft flag and a
rollup that errors each hold the request, because the reply tells colleagues the
merge request is reviewable and an unverified one is exactly the claim this rule
exists to keep off the channel.

Ships INERT behind ``review_resume_reply_enabled`` (default false): nothing
reaches a colleague thread until an overlay opts in.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.backend_protocols import CodeHostBackend, DraftState, MessagingBackend
from teatree.core.gates.review_request_draft_gate import draft_state
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.models import ReviewRequestPost
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.core.review.review_pause import PauseState, read_pause_state
from teatree.loop.scanners.base import ScanSignal
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

#: The reply the owner would have typed themselves — one line, no signature, no
#: footer, nothing that reads as machine-generated on a colleague's thread.
RESUME_REPLY_TEXT = "Now ready for review."

RESUME_POST_ACTION = "review_request_resume_post"

_CHECKS_GREEN = "green"


def _claim_resume(post: ReviewRequestPost, claimed_at: dt.datetime) -> bool:
    return ReviewRequestPost.objects.filter(pk=post.pk, resumed_at__isnull=True).update(resumed_at=claimed_at) == 1


def _release_resume(post: ReviewRequestPost, claimed_at: dt.datetime) -> None:
    ReviewRequestPost.objects.filter(pk=post.pk, resumed_at=claimed_at).update(resumed_at=None)


def _required_checks_green(mr_url: str, host: CodeHostBackend) -> bool:
    """Resume ONLY on a proven-green rollup — a whitelist, deliberately not a red denylist.

    Every other verdict holds: ``failed``, ``pending``, and ``unreadable`` (the
    rollup could not be read, so nothing was proven about it). Keeping this an
    equality test against GREEN is what makes a newly-introduced verdict fail
    closed here by construction instead of by someone remembering to add it.
    """
    ref = pr_ref_from_url(mr_url)
    if ref is None:
        logger.warning("review_request_resume: unparsable merge request URL %s — holding the resume", mr_url)
        return False
    try:
        return CodeHostQuery(ref=ref, backend=host).required_checks_status() == _CHECKS_GREEN
    except Exception:
        logger.exception("review_request_resume: required-checks read failed for %s — holding the resume", mr_url)
        return False


def _claim_and_reply(post: ReviewRequestPost, messaging: MessagingBackend) -> ScanSignal | None:
    """Claim the one-shot, reply in the tracked thread, release the claim on any refusal.

    One ``claimed_at`` governs both the claim and its release, so the conditional
    release can never miss the row it wrote. ``None`` means another tick holds the
    claim and this one stands down without posting.
    """
    claimed_at = timezone.now()
    if not _claim_resume(post, claimed_at):
        return None
    try:
        OnBehalfSlackEgress(messaging).post(
            channel=post.slack_channel_id,
            text=RESUME_REPLY_TEXT,
            target=post.mr_url,
            action=RESUME_POST_ACTION,
            thread_ts=post.slack_thread_ts,
            destination=f"review-request thread for {post.mr_url}",
            summary="now ready for review",
        )
    except OnBehalfPostBlockedError as blocked:
        _release_resume(post, claimed_at)
        return ScanSignal(
            kind="review_request.resume_gated",
            summary=str(blocked),
            payload={"mr_url": post.mr_url, "post_id": post.pk},
        )
    except Exception as exc:
        _release_resume(post, claimed_at)
        logger.exception("review_request_resume: reply failed for %s on %s", post.mr_url, post.slack_channel_id)
        return ScanSignal(
            kind="review_request.resume_failed",
            summary=f"Slack resume reply failed for {post.mr_url}: {exc}",
            payload={"mr_url": post.mr_url, "error": str(exc), "post_id": post.pk},
        )
    return ScanSignal(
        kind="review_request.resumed",
        summary=f"Replied in thread — {post.mr_url} is ready for review again",
        payload={"mr_url": post.mr_url, "post_id": post.pk},
    )


@dataclass(slots=True)
class ReviewRequestResumeScanner:
    """Reply "now ready for review" in a paused request's own thread once its MR goes green.

    Stateless beyond the ``ReviewRequestPost`` rows it walks: ``resumed_at``
    carries the whole idempotency contract, so the scanner is safe on every tick.
    """

    messaging: MessagingBackend | None
    host: CodeHostBackend | None = None
    overlay: str = ""
    name: str = "review_request_resume"

    def scan(self) -> list[ScanSignal]:
        messaging = self.messaging
        host = self.host
        if messaging is None or host is None:
            return []
        if not get_effective_settings(self.overlay or None).review_resume_reply_enabled:
            return []
        open_rows = ReviewRequestPost.objects.filter(done_at__isnull=True, resumed_at__isnull=True)
        signals = (self._process_one(post, messaging, host) for post in open_rows.order_by("created_at"))
        return [signal for signal in signals if signal is not None]

    def _process_one(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        host: CodeHostBackend,
    ) -> ScanSignal | None:
        pause = read_pause_state(post, messaging)
        if pause is PauseState.UNKNOWN:
            return ScanSignal(
                kind="review_request.pause_unreadable",
                summary=f"Pause state for {post.mr_url} is unreadable — holding its resume reply",
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )
        if pause is not PauseState.PAUSED or not self._is_ready(post.mr_url, host):
            return None
        return _claim_and_reply(post, messaging)

    def _is_ready(self, mr_url: str, host: CodeHostBackend) -> bool:
        if draft_state(mr_url, overlay_name=self.overlay) is not DraftState.NOT_DRAFT:
            return False
        return _required_checks_green(mr_url, host)
