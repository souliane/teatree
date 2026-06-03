""":merge: reaction on a merged review-request's Slack message (#1797).

When the agent posts a review-request to Slack it tracks the message in a
``ReviewRequestPost`` row (channel + thread ts). When the requested MR
later merges, a ``:merge:`` reaction on that original message closes the
loop visually — reviewers see at a glance which requests have landed,
instead of merged requests looking still-open in the channel.

This scanner is the merge-audit half of the review-request ledger,
distinct from the fibonacci nag (:mod:`teatree.loop.scanners.review_nag`):
the nag is noise the user opts into, the merge reaction is a low-noise
positive signal that always runs. Both walk the same ``ReviewRequestPost``
rows and both mark a row ``done_at`` when its MR leaves the open state, so
whichever fires first stops the other from acting on the same row.

Token routing follows the #1750 rule via
:meth:`MessagingBackend.react_routed` — reacting follows the same posting
rules as messaging: a reaction on a colleague/channel message goes out
under the personal ``xoxp`` token, a reaction on the agent's own DM goes
out under the bot. A personal token that lacks ``reactions:write`` returns
``missing_scope``; the scanner surfaces that as a signal and closes the
row (the reaction can never land, so retrying it every tick is pure
churn) rather than crashing the tick.

Idempotency: ``done_at`` is the claim. It is set with an atomic
conditional ``UPDATE`` *before* the Slack call, so two concurrent ticks
cannot both react — the tick that loses the claim matches zero rows and
skips. A transient Slack failure (``not_in_channel``, a raised transport
error) releases the claim so a future tick retries; a definitive
``missing_scope`` keeps the row closed.
"""

import logging
from dataclasses import dataclass

from django.utils import timezone

from teatree.backends.protocols import CodeHostBackend, MessagingBackend, PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

MERGE_REACTION_EMOJI = "merge"

_MISSING_SCOPE_ERRORS = frozenset({"missing_scope", "no_permission"})
_REACTION_PRESENT_ERRORS = frozenset({"already_reacted"})


@dataclass(slots=True)
class ReviewRequestMergeReactScanner:
    """React ``:merge:`` on the review-request message once its MR merges (#1797).

    Walks open ``ReviewRequestPost`` rows; for each merged MR it reacts on
    the tracked Slack message and marks the row done so the reaction lands
    exactly once. Stateless beyond the rows it walks — safe to run every
    tick regardless of the nag flag.
    """

    messaging: MessagingBackend | None
    host: CodeHostBackend | None = None
    name: str = "review_request_merge_react"

    def scan(self) -> list[ScanSignal]:
        messaging = self.messaging
        host = self.host
        if messaging is None or host is None:
            return []
        signals: list[ScanSignal] = []
        for post in ReviewRequestPost.objects.filter(done_at__isnull=True).order_by("created_at"):
            signal = self._process_one(post, messaging, host)
            if signal is not None:
                signals.append(signal)
        return signals

    def _process_one(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        host: CodeHostBackend,
    ) -> ScanSignal | None:
        if not post.slack_thread_ts:
            return None
        if self._open_state(host, post.mr_url) is not PrOpenState.MERGED:
            return None
        if not self._claim(post):
            return None
        return self._react(post, messaging)

    @staticmethod
    def _open_state(host: CodeHostBackend, mr_url: str) -> PrOpenState:
        """Open-state lookup that never crashes a tick — failures map to ``UNKNOWN``."""
        try:
            return host.get_pr_open_state(pr_url=mr_url)
        except Exception as exc:  # noqa: BLE001 — backend lookup must never crash a tick.
            logger.warning("review_request_merge_react: open-state lookup failed for %s: %s", mr_url, exc)
            return PrOpenState.UNKNOWN

    @staticmethod
    def _claim(post: ReviewRequestPost) -> bool:
        """Atomically claim the row by setting ``done_at`` only if still open.

        The conditional ``UPDATE`` is the lock: the winning tick gets
        ``updated == 1`` and reacts; a concurrent tick that already claimed
        gets ``0`` and skips, so exactly one ``:merge:`` reaction is posted.
        Released by :meth:`_release` on a transient Slack failure.
        """
        updated = ReviewRequestPost.objects.filter(pk=post.pk, done_at__isnull=True).update(done_at=timezone.now())
        return updated == 1

    @staticmethod
    def _release(post: ReviewRequestPost) -> None:
        """Release the claim so a future tick retries the reaction."""
        ReviewRequestPost.objects.filter(pk=post.pk).update(done_at=None)

    def _react(self, post: ReviewRequestPost, messaging: MessagingBackend) -> ScanSignal:
        try:
            response = messaging.react_routed(
                channel=post.slack_channel_id,
                ts=post.slack_thread_ts,
                emoji=MERGE_REACTION_EMOJI,
            )
        except Exception as exc:  # noqa: BLE001 — a Slack transport error must not crash the tick.
            self._release(post)
            logger.warning(
                "review_request_merge_react: react failed for %s on %s/%s: %s",
                post.mr_url,
                post.slack_channel_id,
                post.slack_thread_ts,
                exc,
            )
            return ScanSignal(
                kind="review_request_merge_react.react_failed",
                summary=f"Slack :merge: reaction failed for {post.mr_url}: {exc}",
                payload={"mr_url": post.mr_url, "error": str(exc), "post_id": post.pk},
            )
        return self._signal_for_response(post, response)

    def _signal_for_response(self, post: ReviewRequestPost, response: RawAPIDict) -> ScanSignal:
        error = str(response.get("error") or "")
        if response.get("ok") or error in _REACTION_PRESENT_ERRORS:
            return ScanSignal(
                kind="review_request_merge_react.reacted",
                summary=f"Reacted :merge: on review-request for {post.mr_url}",
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )
        if error in _MISSING_SCOPE_ERRORS:
            needed = str(response.get("needed") or "reactions:write")
            return ScanSignal(
                kind="review_request_merge_react.missing_scope",
                summary=(
                    f":merge: reaction for {post.mr_url} skipped — the personal xoxp token "
                    f"is missing the {needed!r} scope. Re-run `t3 setup slack-user-token`."
                ),
                payload={"mr_url": post.mr_url, "needed": needed, "post_id": post.pk},
            )
        self._release(post)
        return ScanSignal(
            kind="review_request_merge_react.react_failed",
            summary=f"Slack :merge: reaction refused for {post.mr_url}: {error or 'unknown_error'}",
            payload={"mr_url": post.mr_url, "error": error or "unknown_error", "post_id": post.pk},
        )
