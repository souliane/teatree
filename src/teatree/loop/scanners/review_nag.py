"""Widening ``@engineers :pray:`` re-ping for unreviewed MRs in the review channel (#1084 follow-up).

The user posts MRs to the review channel; the bot tracks each in a
``ReviewRequestPost`` row. This scanner walks the open rows each tick and, when
an MR has had **no activity for its current re-ask interval** — no thread reply,
no reaction — and is still live-open, non-draft, unapproved and unpaused, posts
exactly ONE thread reply: the ``@engineers`` subteam mention + `` :pray:`` on
``(channel, thread_ts)``. The post stays behind the #960 on-behalf gate
(``OnBehalfSlackEgress``).

Cadence. The interval is ``TriageThresholds.nag_interval_for_attempt(owner,
nag_count)`` — the repo owner's base interval times the Fibonacci step for the
number of re-asks already made, capped at ``review_nag_max_interval_days``. An
engineering repo therefore waits 2, 2, 4, 6, 10, 16, 26 days between re-asks
rather than pinging the same group every two days forever. Once the schedule
reaches its ceiling the group has been asked enough: the scanner records an
owner question (``ask_mr_state``) instead of nagging again.

Activity is read LIVE via ``conversations.replies`` (``fetch_thread_replies``,
the same messaging backend the nag posts with):
``last_activity = max(post_ts, latest reply ts, reaction-present ⇒ now)``.
Slack exposes no per-reaction timestamp, so a reaction on any thread message
counts as fresh engagement and suppresses the nag.

Merged/closed safety: a MERGED MR routes through ``react_merge_on_post`` so the
``:merge:`` reaction still lands; a CLOSED MR is marked done.

Everything else FAILS CLOSED, because the harmful direction here is POSTING: the
re-ping is a colleague-visible @-mention of a whole group, so an MR whose state
nothing could establish must not be nagged about. A DRAFT, an APPROVED, a PAUSED
MR and every unreadable answer — no code-host backend, an ``UNKNOWN`` open state,
a failed open-state lookup, an unparsable URL, an ``UNKNOWN`` draft state, an
``UNKNOWN`` approval state, an ``UNKNOWN`` pause state — all SKIP without closing
the row, so a later merge-react still fires and the next tick retries once the
read works again.

Disabled by default: only runs when ``review_nag_enabled`` is true.

Concurrency: two ticks both observe the same ``last_nag_at`` and would each
post. The nag is claimed with an atomic conditional ``UPDATE`` (``last_nag_at``
and ``nag_count`` advanced only if both still equal the observed values) *before*
the post — the tick that loses the claim skips, so exactly one re-ping fires per
window. A blocked or failed post restores BOTH columns, so a re-ask that never
reached Slack never widens the schedule.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.backend_protocols import ApprovalReadState, CodeHostBackend, DraftState, MessagingBackend, PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.core.review.mr_state_question import ask_mr_state
from teatree.core.review.mr_triage import DEFAULT_THRESHOLDS, RepoOwner, TriageThresholds
from teatree.core.review.review_pause import PauseState, read_pause_state
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.review_request_merge_react import react_merge_on_post
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

_BACKOFF_EXHAUSTED_OPTIONS = (
    "Ping the group again anyway",
    "It is waiting on me — stop asking",
    "Close it",
)


def default_repo_owner(_slug: str) -> RepoOwner:
    """The no-ownership-model answer: every repo is on the engineering cadence.

    Deliberately NOT the ladder's fail-safe. :class:`MrFacts` defaults to the PATIENT
    owner because a triage verdict built from missing facts must not act; here the
    default is the interval this scanner has always enforced, so an overlay that
    declares no ownership keeps the cadence it shipped with. An overlay that HAS a
    model supplies its own resolver, and owns its own unknown-repo answer.
    """
    return RepoOwner.ENGINEERING


def _epoch(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


@dataclass(slots=True)
class ReviewNagScanner:
    """Walk ``ReviewRequestPost`` rows and re-ping ``@engineers`` on a widening schedule.

    Stateless beyond the DB rows it walks. Safe to invoke from every loop tick —
    at most one re-ping per row per window, enforced by the ``last_nag_at`` /
    ``nag_count`` claim.

    ``thresholds`` supplies the per-owner base intervals; its ceiling is always
    re-resolved from ``review_nag_max_interval_days`` at scan time, so the
    operator's setting — not an injected constant — is what bounds the backoff.
    """

    messaging: MessagingBackend | None
    host: CodeHostBackend | None = None
    identities: tuple[str, ...] = field(default_factory=tuple)
    now: dt.datetime | None = None
    thresholds: TriageThresholds = DEFAULT_THRESHOLDS
    repo_owner: Callable[[str], RepoOwner] = default_repo_owner
    name: str = "review_nag"

    def scan(self) -> list[ScanSignal]:
        settings = get_effective_settings()
        if not settings.review_nag_enabled:
            return []
        messaging = self.messaging
        if messaging is None:
            return []
        thresholds = replace(
            self.thresholds,
            nag_backoff_cap=dt.timedelta(days=settings.review_nag_max_interval_days),
        )
        right_now = self.now or timezone.now()
        signals: list[ScanSignal] = []
        for post in ReviewRequestPost.objects.filter(done_at__isnull=True).order_by("created_at"):
            signal = self._process_one(post, messaging, right_now, thresholds)
            if signal is not None:
                signals.append(signal)
        return signals

    def _process_one(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
        thresholds: TriageThresholds,
    ) -> ScanSignal | None:
        owner = self._repo_owner_for(post)
        interval = thresholds.nag_interval_for_attempt(owner, post.nag_count)
        if post.last_nag_at is not None and right_now - post.last_nag_at < interval:
            return None
        last_activity = self._last_activity(post, messaging, right_now)
        if last_activity is None:
            return None  # activity read unavailable — skip this tick, retry later
        if right_now - last_activity <= interval:
            return None  # recent thread reply / reaction — no re-ping
        blocked = self._mr_not_naggable(post, messaging, right_now)
        if blocked is not None:
            return blocked
        if thresholds.nag_backoff_at_cap(owner, post.nag_count):
            return _ask_owner_for_state(post, interval)
        return self._post_engineers_pray(post, messaging, right_now)

    def _repo_owner_for(self, post: ReviewRequestPost) -> RepoOwner:
        """Which org function reviews this MR's repo — the base interval's selector."""
        ref = pr_ref_from_url(post.mr_url)
        return self.repo_owner(ref.slug if ref is not None else "")

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
        """A skip-signal when the MR must not be nagged about; ``None`` when naggable.

        MERGED routes through :func:`react_merge_on_post` (the ``:merge:``
        reaction still lands); CLOSED marks the row done. Every other non-naggable
        answer — DRAFT, APPROVED, PAUSED, and each unreadable state — SKIPS
        without closing the row, so a later merge-react still fires and the next
        tick retries.

        The whole guard fails CLOSED. The re-ping @-mentions a group of
        colleagues, so no unverifiable read may license one: a missing code-host
        backend, a failed open-state lookup and an ``UNKNOWN`` open state each
        skip rather than proceed.
        """
        host = self.host
        if host is None:
            return _unreadable_state_skip(post, "no code-host backend is configured")
        settled = self._open_state_skip(post, messaging, right_now, host)
        if settled is not None:
            return settled
        paused = _pause_skip(post, messaging)
        if paused is not None:
            return paused
        return _draft_or_approved_skip(post, host)

    def _open_state_skip(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
        host: CodeHostBackend,
    ) -> ScanSignal | None:
        """The forge's open state, when it alone already decides this row's fate.

        ``None`` means only that the MR is confirmed live-open — the social
        checks below still get their say.
        """
        try:
            open_state = host.get_pr_open_state(pr_url=post.mr_url)
        except Exception as exc:  # noqa: BLE001 — backend lookup must never crash a tick.
            logger.warning("review_nag: open-state lookup failed for %s: %s", post.mr_url, exc)
            return _unreadable_state_skip(post, f"the open-state lookup failed: {exc}")
        if open_state is PrOpenState.MERGED:
            return react_merge_on_post(post, messaging, host=host, identities=self.identities)
        if open_state is PrOpenState.CLOSED:
            post.done_at = right_now
            post.save(update_fields=["done_at"])
            return ScanSignal(
                kind="review_nag.mr_closed",
                summary=f"Review-request post for {post.mr_url} closed — MR is closed",
                payload={"mr_url": post.mr_url, "post_id": post.pk, "open_state": open_state.value},
            )
        if open_state is not PrOpenState.OPEN:
            return _unreadable_state_skip(post, f"the open state came back {open_state.value}")
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

        claim = _NagClaim.take(post, right_now)
        if claim is None:
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
                summary=f"re-ping #{claim.nag_count}",
            )
        except OnBehalfPostBlockedError as blocked:
            claim.release()
            return ScanSignal(
                kind="review_nag.gated",
                summary=str(blocked),
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )
        except Exception as exc:  # noqa: BLE001 — Slack-Connect not_in_channel etc.; release the claim for retry.
            claim.release()
            logger.warning("review_nag: post failed for %s on %s: %s", post.mr_url, post.slack_channel_id, exc)
            return ScanSignal(
                kind="review_nag.post_failed",
                summary=f"Slack post failed for {post.mr_url}: {exc}",
                payload={"mr_url": post.mr_url, "error": str(exc), "post_id": post.pk},
            )

        post.last_nag_at = right_now
        post.nag_count = claim.nag_count
        return ScanSignal(
            kind="review_nag.ping",
            summary=f"Re-pinged @engineers for {post.mr_url} (re-ask #{claim.nag_count})",
            payload={"mr_url": post.mr_url, "post_id": post.pk, "nag_count": claim.nag_count},
        )


@dataclass(frozen=True, slots=True)
class _NagClaim:
    """The won right to post exactly one re-ping, and the means to give it back.

    ``last_nag_at`` and ``nag_count`` advance together in ONE conditional
    ``UPDATE`` guarded on both observed values, so a losing tick claims neither
    and :meth:`release` can restore the exact pair it displaced. Splitting them
    would let a post that never reached Slack still widen the schedule — a
    permanently-gated row would walk itself out to the ceiling without a single
    re-ask having been delivered.
    """

    post_id: int
    claimed_at: dt.datetime
    nag_count: int
    previous_nag_at: dt.datetime | None
    previous_nag_count: int

    @classmethod
    def take(cls, post: ReviewRequestPost, right_now: dt.datetime) -> "_NagClaim | None":
        claimed = ReviewRequestPost.objects.filter(
            pk=post.pk,
            last_nag_at=post.last_nag_at,
            nag_count=post.nag_count,
        ).update(last_nag_at=right_now, nag_count=post.nag_count + 1)
        if claimed != 1:
            return None
        return cls(
            post_id=post.pk,
            claimed_at=right_now,
            nag_count=post.nag_count + 1,
            previous_nag_at=post.last_nag_at,
            previous_nag_count=post.nag_count,
        )

    def release(self) -> None:
        ReviewRequestPost.objects.filter(
            pk=self.post_id,
            last_nag_at=self.claimed_at,
            nag_count=self.nag_count,
        ).update(last_nag_at=self.previous_nag_at, nag_count=self.previous_nag_count)


def _ask_owner_for_state(post: ReviewRequestPost, interval: dt.timedelta) -> ScanSignal | None:
    """Hand an exhausted re-ask train to the owner instead of pinging the group again.

    ``ask_mr_state`` is idempotent per merge request and bounded per tick, so a
    row that stays at the ceiling re-offers the same question rather than
    accumulating them; ``None`` (the per-tick cap refused it) leaves the row
    untouched for a later tick.
    """
    reason = (
        f"it has been re-asked {post.nag_count} times with no reply, "
        f"and the backoff has reached its {interval.days}-day ceiling."
    )
    question = ask_mr_state(mr_url=post.mr_url, reason=reason, options=_BACKOFF_EXHAUSTED_OPTIONS)
    if question is None:
        return None
    return ScanSignal(
        kind="review_nag.backoff_exhausted",
        summary=f"Asked the owner about {post.mr_url} — re-asked to the cap with no reply",
        payload={"mr_url": post.mr_url, "post_id": post.pk, "nag_count": post.nag_count},
    )


def _draft_or_approved_skip(post: ReviewRequestPost, host: CodeHostBackend) -> ScanSignal | None:
    """A skip-signal when the MR is a draft, is approved, or answers neither honestly."""
    ref = pr_ref_from_url(post.mr_url)
    if ref is None:
        return _unreadable_state_skip(post, "the merge-request URL does not parse")
    draft = _draft_state(host, ref.slug, ref.pr_id)
    if draft is not DraftState.NOT_DRAFT:
        return ScanSignal(
            kind="review_nag.mr_draft",
            summary=f"Skipping nag for {post.mr_url} — MR draft state is {draft.value}",
            payload={"mr_url": post.mr_url, "post_id": post.pk, "draft_state": draft.value},
        )
    approval = _approval_state(host, ref.slug, ref.pr_id)
    if approval is ApprovalReadState.APPROVED:
        return ScanSignal(
            kind="review_nag.mr_approved",
            summary=f"Skipping nag for {post.mr_url} — MR is approved",
            payload={"mr_url": post.mr_url, "post_id": post.pk},
        )
    if approval is ApprovalReadState.UNKNOWN:
        return ScanSignal(
            kind="review_nag.mr_approval_unknown",
            summary=f"Skipping nag for {post.mr_url} — approval state is unreadable",
            payload={"mr_url": post.mr_url, "post_id": post.pk, "approval_state": approval.value},
        )
    return None


def _unreadable_state_skip(post: ReviewRequestPost, why: str) -> ScanSignal:
    return ScanSignal(
        kind="review_nag.mr_state_unreadable",
        summary=f"Skipping nag for {post.mr_url} — {why}",
        payload={"mr_url": post.mr_url, "post_id": post.pk, "reason": why},
    )


def _pause_skip(post: ReviewRequestPost, messaging: MessagingBackend) -> ScanSignal | None:
    """A skip-signal when the owner is holding this request, or when that cannot be read.

    The hold is the owner's own reaction on the thread root; escalating past it
    would @-mention a group about work its author has said is not ready. An
    ``UNKNOWN`` pause state is treated the same way, since a hold nobody can read
    is exactly the case where the mechanism would silently stop working.
    """
    state = read_pause_state(post, messaging)
    if state is PauseState.NOT_PAUSED:
        return None
    return ScanSignal(
        kind="review_nag.mr_paused",
        summary=f"Skipping nag for {post.mr_url} — pause state is {state.value}",
        payload={"mr_url": post.mr_url, "post_id": post.pk, "pause_state": state.value},
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


def _draft_state(host: CodeHostBackend, slug: str, pr_id: int) -> DraftState:
    try:
        return host.fetch_pr_draft_state(slug=slug, pr_id=pr_id)
    except Exception as exc:  # noqa: BLE001 — a draft probe must never crash a tick.
        logger.warning("review_nag: draft probe failed for %s#%s: %s", slug, pr_id, exc)
        return DraftState.UNKNOWN


def _approval_state(host: CodeHostBackend, slug: str, pr_id: int) -> ApprovalReadState:
    try:
        state = host.get_mr_approvals(repo=slug, pr_iid=pr_id)
    except Exception as exc:  # noqa: BLE001 — an approval probe must never crash a tick.
        logger.warning("review_nag: approval probe failed for %s#%s: %s", slug, pr_id, exc)
        return ApprovalReadState.UNKNOWN
    return ApprovalReadState.APPROVED if state.get("approved_by") else ApprovalReadState.NOT_APPROVED


def _engineers_mention(messaging: MessagingBackend) -> str:
    try:
        usergroup_id = messaging.resolve_user_id("engineers")
    except Exception:  # noqa: BLE001 — never crash on a lookup failure.
        usergroup_id = ""
    if usergroup_id:
        return f"<!subteam^{usergroup_id}>"
    return "@engineers"
