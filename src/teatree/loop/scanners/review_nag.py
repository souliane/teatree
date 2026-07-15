"""2-day ``@engineers :pray:`` re-ping for unreviewed MRs in the review channel (#1084 follow-up).

The user posts MRs to the review channel; the bot tracks each in a
``ReviewRequestPost`` row. This scanner walks the open rows each tick and,
when an MR has had **no activity for 2 days** — no thread reply, no reaction —
and is still live-open, non-draft, and unapproved, posts exactly ONE thread
reply: the ``@engineers`` subteam mention + `` :pray:`` on ``(channel,
thread_ts)``. ``last_nag_at`` enforces "no double-ping within 2 days"; the post
stays behind the #960 on-behalf gate (``OnBehalfSlackEgress``).

Activity is read LIVE via ``conversations.replies`` (``fetch_thread_replies``,
the same messaging backend the nag posts with):
``last_activity = max(post_ts, latest reply ts, reaction-present ⇒ now)``.
Slack exposes no per-reaction timestamp, so a reaction on any thread message
counts as fresh engagement and suppresses the nag.

Merged/closed/draft/approved safety: a MERGED MR routes through
``react_merge_on_post`` so the ``:merge:`` reaction still lands; a CLOSED MR is
marked done; a live DRAFT MR is SKIPPED (a draft is not ready for review) and
an APPROVED MR is SKIPPED (review already happened) — neither is closed, so a
later merge-react still fires. UNKNOWN state / missing backend fails open — the
scanner never wedges on an unverifiable read.

Disabled by default: only runs when ``review_nag_enabled`` is true.

Concurrency: two ticks both observe the same ``last_nag_at`` and would each
post. The nag is claimed with an atomic conditional ``UPDATE`` (``last_nag_at``
advanced only if it still equals the observed value) *before* the post — the
tick that loses the claim skips, so exactly one re-ping fires per window.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field

from django.utils import timezone

from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend, PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.review_request_merge_react import react_merge_on_post

logger = logging.getLogger(__name__)

_NAG_INTERVAL = dt.timedelta(days=2)


def _epoch(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


@dataclass(slots=True)
class ReviewNagScanner:
    """Walk ``ReviewRequestPost`` rows and re-ping ``@engineers`` after 2 idle days.

    Stateless beyond the DB rows it walks. Safe to invoke from every loop
    tick — at most one re-ping per row per 2-day window, enforced by the
    ``last_nag_at`` column.
    """

    messaging: MessagingBackend | None
    host: CodeHostBackend | None = None
    identities: tuple[str, ...] = field(default_factory=tuple)
    now: dt.datetime | None = None
    name: str = "review_nag"

    def scan(self) -> list[ScanSignal]:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: loaded at tick time, not import

        if not get_effective_settings().review_nag_enabled:
            return []
        messaging = self.messaging
        if messaging is None:
            return []
        right_now = self.now or timezone.now()
        signals: list[ScanSignal] = []
        for post in ReviewRequestPost.objects.filter(done_at__isnull=True).order_by("created_at"):
            signal = self._process_one(post, messaging, right_now)
            if signal is not None:
                signals.append(signal)
        return signals

    def _process_one(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
    ) -> ScanSignal | None:
        if post.last_nag_at is not None and right_now - post.last_nag_at < _NAG_INTERVAL:
            return None
        last_activity = self._last_activity(post, messaging, right_now)
        if last_activity is None:
            return None  # activity read unavailable — skip this tick, retry later
        if right_now - last_activity <= _NAG_INTERVAL:
            return None  # recent thread reply / reaction — no re-ping
        blocked = self._mr_not_naggable(post, messaging, right_now)
        if blocked is not None:
            return blocked
        return self._post_engineers_pray(post, messaging, right_now)

    @staticmethod
    def _last_activity(
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
    ) -> dt.datetime | None:
        """``max(post_ts, latest reply ts, reaction-present ⇒ now)``; ``None`` on read failure."""
        try:
            replies = messaging.fetch_thread_replies(channel=post.slack_channel_id, thread_ts=post.slack_thread_ts)
        except Exception as exc:  # noqa: BLE001 — a thread read must never crash a tick.
            logger.warning("review_nag: thread read failed for %s: %s", post.mr_url, exc)
            return None
        epochs = [_epoch(post.slack_thread_ts)]
        for msg in replies:
            if not isinstance(msg, dict):
                continue
            if msg.get("reactions"):
                return right_now  # a reaction is fresh engagement; Slack carries no reaction ts
            ts = msg.get("ts")
            if isinstance(ts, str):
                epochs.append(_epoch(ts))
        latest = max((e for e in epochs if e > 0.0), default=0.0)
        if latest <= 0.0:
            return post.created_at
        return dt.datetime.fromtimestamp(latest, tz=dt.UTC)

    def _mr_not_naggable(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
    ) -> ScanSignal | None:
        """A skip-signal when the MR is merged/closed/draft/approved; ``None`` when naggable.

        MERGED routes through :func:`react_merge_on_post` (the ``:merge:``
        reaction still lands); CLOSED marks the row done. A live DRAFT or an
        APPROVED MR is SKIPPED without closing the row — it is not ready for /
        no longer needs a nag, but a later merge-react must still fire. Fails
        open: no backend, an ``UNKNOWN`` open-state, or an unparsable URL
        returns ``None`` and the nag proceeds — the guard never wedges on an
        unverifiable state.
        """
        if self.host is None:
            return None
        try:
            open_state = self.host.get_pr_open_state(pr_url=post.mr_url)
        except Exception as exc:  # noqa: BLE001 — backend lookup must never crash a tick.
            logger.warning("review_nag: open-state lookup failed for %s: %s", post.mr_url, exc)
            return None
        if open_state is PrOpenState.MERGED:
            return react_merge_on_post(post, messaging, host=self.host, identities=self.identities)
        if open_state is PrOpenState.CLOSED:
            post.done_at = right_now
            post.save(update_fields=["done_at"])
            return ScanSignal(
                kind="review_nag.mr_closed",
                summary=f"Review-request post for {post.mr_url} closed — MR is closed",
                payload={"mr_url": post.mr_url, "post_id": post.pk, "open_state": open_state.value},
            )
        return self._draft_or_approved_skip(post)

    def _draft_or_approved_skip(self, post: ReviewRequestPost) -> ScanSignal | None:
        from teatree.utils.url_slug import pr_ref_from_url  # noqa: PLC0415 — deferred: keeps scanner import light

        ref = pr_ref_from_url(post.mr_url)
        host = self.host
        if ref is None or host is None:
            return None
        if _is_draft(host, ref.slug, ref.pr_id):
            return ScanSignal(
                kind="review_nag.mr_draft",
                summary=f"Skipping nag for {post.mr_url} — MR is a draft",
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )
        if _is_approved(host, ref.slug, ref.pr_id):
            return ScanSignal(
                kind="review_nag.mr_approved",
                summary=f"Skipping nag for {post.mr_url} — MR is approved",
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )
        return None

    @staticmethod
    def _post_engineers_pray(
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
    ) -> ScanSignal | None:
        reconciled = _consult_guard_before_nag(post)
        if reconciled is not None:
            return reconciled

        observed = post.last_nag_at
        updated = ReviewRequestPost.objects.filter(pk=post.pk, last_nag_at=observed).update(last_nag_at=right_now)
        if updated != 1:
            return None

        text = f"{_engineers_mention(messaging)} :pray:"
        try:
            OnBehalfSlackEgress(messaging).post(
                channel=post.slack_channel_id,
                text=text,
                target=post.mr_url,
                action="review_nag_post",
                thread_ts=post.slack_thread_ts,
                destination=f"review-request thread for {post.mr_url}",
                summary="2-day re-ping",
            )
        except OnBehalfPostBlockedError as blocked:
            ReviewRequestPost.objects.filter(pk=post.pk, last_nag_at=right_now).update(last_nag_at=observed)
            return ScanSignal(
                kind="review_nag.gated",
                summary=str(blocked),
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )
        except Exception as exc:  # noqa: BLE001 — Slack-Connect not_in_channel etc.; release the claim for retry.
            ReviewRequestPost.objects.filter(pk=post.pk, last_nag_at=right_now).update(last_nag_at=observed)
            logger.warning("review_nag: post failed for %s on %s: %s", post.mr_url, post.slack_channel_id, exc)
            return ScanSignal(
                kind="review_nag.post_failed",
                summary=f"Slack post failed for {post.mr_url}: {exc}",
                payload={"mr_url": post.mr_url, "error": str(exc), "post_id": post.pk},
            )

        post.last_nag_at = right_now
        return ScanSignal(
            kind="review_nag.ping",
            summary=f"Re-pinged @engineers for {post.mr_url} (2 idle days)",
            payload={"mr_url": post.mr_url, "post_id": post.pk},
        )


def _consult_guard_before_nag(post: ReviewRequestPost) -> ScanSignal | None:
    """Live-read dedup before nagging (#1084).

    If the review was already requested again / picked up out-of-band (a user
    or another actor re-posted the MR URL in the channel window), reconcile the
    row (``done_at`` set, PR transitioned) and skip the nag so the train stops.
    Fails open: a missing channel/token or a slow/failed read returns ``None``
    and the nag proceeds — the guard must never wedge the loop on a Slack read.
    """
    from teatree.core.gates.review_request_guard import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
        reconcile_out_of_band,
        resolve_guard_target,
    )

    target = resolve_guard_target(channel_id=post.slack_channel_id)
    if target is None:
        return None
    permalink = reconcile_out_of_band(mr_url=post.mr_url, target=target)
    if not permalink:
        return None
    return ScanSignal(
        kind="review_nag.reconciled",
        summary=f"Review for {post.mr_url} already requested out-of-band — nag train stopped",
        payload={"mr_url": post.mr_url, "permalink": permalink, "post_id": post.pk},
    )


def _is_draft(host: CodeHostBackend, slug: str, pr_id: int) -> bool:
    try:
        return bool(host.fetch_pr_is_draft(slug=slug, pr_id=pr_id))
    except Exception as exc:  # noqa: BLE001 — a draft probe must never crash a tick.
        logger.warning("review_nag: draft probe failed for %s#%s: %s", slug, pr_id, exc)
        return False


def _is_approved(host: CodeHostBackend, slug: str, pr_id: int) -> bool:
    try:
        state = host.get_mr_approvals(repo=slug, pr_iid=pr_id)
    except Exception as exc:  # noqa: BLE001 — an approval probe must never crash a tick.
        logger.warning("review_nag: approval probe failed for %s#%s: %s", slug, pr_id, exc)
        return False
    return bool(state.get("approved_by"))


def _engineers_mention(messaging: MessagingBackend) -> str:
    try:
        usergroup_id = messaging.resolve_user_id("engineers")
    except Exception:  # noqa: BLE001 — never crash on a lookup failure.
        usergroup_id = ""
    if usergroup_id:
        return f"<!subteam^{usergroup_id}>"
    return "@engineers"
