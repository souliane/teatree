"""Widening re-ask for unreviewed MRs in the review channel (#1084 follow-up).

The user posts MRs to the review channel; the bot tracks each in a
``ReviewRequestPost`` row. This scanner walks the open rows each tick and, when
an MR has had **no activity for its current re-ask interval** — no thread reply,
no qualifying reaction — and is still live-open, non-draft, unapproved and unpaused, posts
one thread reply for every batch thread: a safe reviewer or configured-group mention
and a short re-ask on ``(channel, thread_ts)``. A missing safe mention refuses the post.
The post stays behind the #960 on-behalf gate
(``OnBehalfSlackEgress``).

Mention. :mod:`teatree.loop.scanners.review_nag_mention` maps engaged forge users
only when one active, non-bot, non-guest Slack user matches, then falls back to the
per-overlay ``review_nag_reask_mention`` setting when nobody maps.

Cadence. The interval is ``TriageThresholds.nag_interval_for_attempt(owner,
nag_count)`` — the repo owner's base interval times the Fibonacci step for the
number of re-asks already made, capped at ``review_nag_max_interval_days``. An
engineering repo therefore waits 2, 2, 4, 6, 10, 16, 26 days between re-asks
rather than pinging the same group every two days forever. Once the schedule
reaches its ceiling the group has been asked enough: the scanner records an
owner question (``ask_mr_state``) instead of nagging again.

Activity is read LIVE via ``conversations.replies`` (``fetch_thread_replies``,
the same messaging backend the nag posts with):
``last_activity = max(post_ts, latest reply ts, reaction not proven requester-only ⇒ now)``.
Slack exposes no per-reaction timestamp. A reaction counts unless its users are
provably only the requester; unreadable users and an unresolved requester id
still count as activity.

Merged/closed safety: a MERGED MR routes through ``react_merge_on_post`` so the
``:merge:`` reaction still lands; a CLOSED MR is marked done.

Everything else FAILS CLOSED, because the harmful direction here is POSTING: the
re-ping may @-mention colleagues, so an MR whose state
nothing could establish must not be nagged about. A DRAFT, an APPROVED, a PAUSED
MR and every unreadable answer — no code-host backend, an ``UNKNOWN`` open state,
a failed open-state lookup, an unparsable URL, an ``UNKNOWN`` draft state, an
``UNKNOWN`` approval state, an ``UNKNOWN`` pause state — all SKIP without closing
the row, so a later merge-react still fires and the next tick retries once the
read works again.

The scanner is selected without a feature flag, but an empty or unreadable safe
mention holds its re-ask and leaves the request open.

Concurrency: one transaction claims every open sibling row in a channel/thread
before posting. A later tick sees the shared stamp and skips. A blocked or failed
post restores each row's previous stamp and count; a confirmed reply writes its
timestamp to every sibling in one update.
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
from teatree.core.review.repo_exemption import mr_url_is_review_exempt
from teatree.core.review.review_candidate import _is_self_authored
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.review_nag_activity import pause_skip as _pause_skip
from teatree.loop.scanners.review_nag_activity import thread_activity
from teatree.loop.scanners.review_nag_claim import ReviewNagClaim, ReviewNagPostReconciliationError
from teatree.loop.scanners.review_nag_mention import RequesterIdentity, engaged_reask_mention
from teatree.loop.scanners.review_request_merge_react import react_merge_on_post
from teatree.on_behalf_gate import OnBehalfContext
from teatree.types import RawAPIDict
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

_BACKOFF_EXHAUSTED_OPTIONS = (
    "Ping the group again anyway",
    "It is waiting on me — stop asking",
    "Close it",
)


@dataclass(frozen=True, slots=True)
class ReaskText:
    text: str
    held_reason: str


def _reask_text(mention: str, mr_url: str) -> ReaskText:
    if not mention:
        return ReaskText("", f"No safe reviewer mention for {mr_url}")
    return ReaskText(f"{mention} could you please have another look?", "")


def default_repo_owner(_slug: str) -> RepoOwner:
    """The no-ownership-model answer: every repo is on the engineering cadence.

    Deliberately NOT the ladder's fail-safe. :class:`MrFacts` defaults to the PATIENT
    owner because a triage verdict built from missing facts must not act; here the
    default is the interval this scanner has always enforced, so an overlay that
    declares no ownership keeps the cadence it shipped with. An overlay that HAS a
    model supplies its own resolver, and owns its own unknown-repo answer.
    """
    return RepoOwner.ENGINEERING


@dataclass(slots=True)
class ReviewNagScanner:
    """Walk ``ReviewRequestPost`` rows and re-ask for review on a widening schedule.

    Stateless beyond the DB rows it walks. Safe to invoke from every loop tick —
    at most one re-ping per thread per window, enforced by the ``last_nag_at`` /
    ``nag_count`` claim.

    ``thresholds`` supplies the per-owner base intervals; its ceiling is always
    re-resolved from ``review_nag_max_interval_days`` at scan time, so the
    operator's setting — not an injected constant — is what bounds the backoff.
    """

    messaging: MessagingBackend | None
    host: CodeHostBackend | None = None
    identities: tuple[str, ...] = field(default_factory=tuple)
    requester_slack_id: str = ""
    now: dt.datetime | None = None
    thresholds: TriageThresholds = DEFAULT_THRESHOLDS
    repo_owner: Callable[[str], RepoOwner] = default_repo_owner
    overlay_name: str = ""
    name: str = "review_nag"

    def scan(self) -> list[ScanSignal]:
        settings = get_effective_settings(self.overlay_name or None)
        messaging = self.messaging
        if messaging is None:
            return []
        thresholds = replace(
            self.thresholds,
            nag_backoff_cap=dt.timedelta(days=settings.review_nag_max_interval_days),
        )
        right_now = self.now or timezone.now()
        signals: list[ScanSignal] = []
        attempted_threads: set[tuple[str, str]] = set()
        rows = ReviewRequestPost.objects.filter(done_at__isnull=True, overlay=self.overlay_name)
        for post in rows.order_by("created_at"):
            thread = (post.slack_channel_id, post.slack_thread_ts)
            if thread in attempted_threads:
                continue
            signal = self._process_one(post, messaging, right_now, thresholds)
            if signal is not None:
                signals.append(signal)
                if signal.kind in {
                    "review_nag.ping",
                    "review_nag.gated",
                    "review_nag.post_failed",
                    "review_nag.mention_held",
                }:
                    attempted_threads.add(thread)
        return signals

    def _process_one(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
        thresholds: TriageThresholds,
    ) -> ScanSignal | None:
        authorship = _is_self_authored(post.mr_url, self.host, self.identities)
        if authorship is not True:
            return _authorship_skip(post, authorship=authorship)
        exempt = _review_exempt_skip(post, self.overlay_name)
        if exempt is not None:
            return exempt
        window = self._thread_due_window(post, messaging, right_now, thresholds)
        if window is None:
            return None
        interval, peers, visible_root = window
        blocked = self._mr_not_naggable(post, messaging, right_now, visible_root=visible_root)
        if blocked is not None:
            return blocked
        if any(thresholds.nag_backoff_at_cap(owner, peer.nag_count) for peer, owner in peers):
            return _ask_owner_for_state(post, interval)
        return self._post_reask(post, messaging, right_now, interval)

    def _thread_due_window(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
        thresholds: TriageThresholds,
    ) -> tuple[dt.timedelta, list[tuple[ReviewRequestPost, RepoOwner]], RawAPIDict | None] | None:
        peers = list(
            ReviewRequestPost.objects.filter(
                overlay=post.overlay,
                slack_channel_id=post.slack_channel_id,
                slack_thread_ts=post.slack_thread_ts,
                done_at__isnull=True,
            )
        )
        if not peers:
            return None
        owned_peers = [(peer, self._repo_owner_for(peer)) for peer in peers]
        interval = max(thresholds.nag_interval_for_attempt(owner, peer.nag_count) for peer, owner in owned_peers)
        if any(peer.last_nag_at is not None and right_now - peer.last_nag_at < interval for peer in peers):
            return None
        observation = self._last_activity(post, messaging, right_now)
        if observation is None:
            return None
        last_activity, visible_root = observation
        return (interval, owned_peers, visible_root) if right_now - last_activity > interval else None

    def _repo_owner_for(self, post: ReviewRequestPost) -> RepoOwner:
        """Which org function reviews this MR's repo — the base interval's selector."""
        ref = pr_ref_from_url(post.mr_url)
        return self.repo_owner(ref.slug if ref is not None else "")

    def _last_activity(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
    ) -> tuple[dt.datetime, RawAPIDict | None] | None:
        """Return last review activity, or ``None`` when the thread read fails."""
        try:
            replies = messaging.fetch_thread_replies(channel=post.slack_channel_id, thread_ts=post.slack_thread_ts)
        except Exception as exc:  # noqa: BLE001 — a thread read must never crash a tick.
            logger.warning("review_nag: thread read failed for %s: %s", post.mr_url, exc)
            return None
        visible_root = next(
            (reply for reply in replies if isinstance(reply, dict) and reply.get("ts") == post.slack_thread_ts),
            None,
        )
        return thread_activity(post, replies, right_now, requester_slack_id=self.requester_slack_id), visible_root

    def _mr_not_naggable(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
        *,
        visible_root: RawAPIDict | None,
    ) -> ScanSignal | None:
        """A skip-signal when the MR must not be nagged about; ``None`` when naggable.

        MERGED routes through :func:`react_merge_on_post` (the ``:merge:``
        reaction still lands); CLOSED marks the row done. Every other non-naggable
        answer — DRAFT, APPROVED, PAUSED, and each unreadable state — SKIPS
        without closing the row, so a later merge-react still fires and the next
        tick retries.

        The whole guard fails CLOSED. The re-ping @-mentions colleagues, so no
        unverifiable read may license one: a missing code-host backend, a failed
        open-state lookup and an ``UNKNOWN`` open state each
        skip rather than proceed.
        """
        host = self.host
        if host is None:
            return _unreadable_state_skip(post, "no code-host backend is configured")
        settled = self._open_state_skip(post, messaging, right_now, host)
        if settled is not None:
            return settled
        paused = _pause_skip(post, messaging, visible_root=visible_root)
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

    def _post_reask(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
        interval: dt.timedelta,
    ) -> ScanSignal | None:
        reconciled = _consult_guard_before_nag(post, self.overlay_name)
        if reconciled is not None:
            return reconciled

        claim = ReviewNagClaim.take(post, right_now, interval)
        if claim is None:
            return None

        try:
            mention = engaged_reask_mention(
                messaging,
                self.host,
                list(ReviewRequestPost.objects.filter(pk__in=[post_id for post_id, _, _ in claim.previous])),
                requester=RequesterIdentity(self.requester_slack_id, self.identities),
                overlay_name=self.overlay_name,
            )
            decision = _reask_text(mention, post.mr_url)
            if decision.held_reason:
                claim.release()
                return ScanSignal(
                    kind="review_nag.mention_held",
                    summary=f"Review re-ask held for {post.mr_url}: {decision.held_reason}",
                    payload={"mr_url": post.mr_url, "error": decision.held_reason, "post_id": post.pk},
                )
            response = OnBehalfSlackEgress(messaging).post(
                channel=post.slack_channel_id,
                text=decision.text,
                target=post.mr_url,
                action="review_nag_post",
                thread_ts=post.slack_thread_ts,
                destination=f"review-request thread for {post.mr_url}",
                summary=f"re-ping #{claim.nag_count}",
                context=OnBehalfContext(overlay=self.overlay_name or None, own_mr=True, target=post.mr_url),
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

        if response.get("ok") is not True:
            claim.release()
            error = str(response.get("error") or "empty response")
            outcome = ScanSignal(
                kind="review_nag.post_failed",
                summary=f"Slack post failed for {post.mr_url}: {error}",
                payload={"mr_url": post.mr_url, "error": error, "post_id": post.pk},
            )
        else:
            reply_ts = str(response.get("ts") or "")
            if not reply_ts:
                msg = f"review nag post for {post.mr_url} succeeded without a reply timestamp"
                raise ReviewNagPostReconciliationError(msg)
            claim.persist_reply_ts(reply_ts)

            post.last_nag_at = right_now
            post.nag_count = claim.nag_count
            post.last_nag_reply_ts = reply_ts
            outcome = ScanSignal(
                kind="review_nag.ping",
                summary=f"Re-asked for review of {post.mr_url} (re-ask #{claim.nag_count})",
                payload={"mr_url": post.mr_url, "post_id": post.pk, "nag_count": claim.nag_count},
            )
        return outcome


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


def _review_exempt_skip(post: ReviewRequestPost, overlay_name: str) -> ScanSignal | None:
    """A skip-signal when the repo's reviews are exempt — the declared never-ask table.

    First of the per-row guards so an exempt repo costs no thread read and takes
    no claim. ``done_at`` is deliberately left unset: the merge-react shares that
    column and still has to close the row when the merge request lands.
    """
    if not mr_url_is_review_exempt(post.mr_url, overlay_name=overlay_name):
        return None
    return ScanSignal(
        kind="review_nag.mr_review_exempt",
        summary=f"Skipping nag for {post.mr_url} — review-exempt repo",
        payload={"mr_url": post.mr_url, "post_id": post.pk},
    )


def _authorship_skip(post: ReviewRequestPost, *, authorship: bool | None) -> ScanSignal:
    if authorship is False:
        kind = "review_nag.foreign_author"
        reason = "the merge request was authored by a colleague"
    else:
        kind = "review_nag.authorship_unreadable"
        reason = "owner authorship could not be proved from the forge"
    return ScanSignal(
        kind=kind,
        summary=f"Skipping nag for {post.mr_url} — {reason}",
        payload={"mr_url": post.mr_url, "post_id": post.pk, "reason": reason},
    )


def _unreadable_state_skip(post: ReviewRequestPost, why: str) -> ScanSignal:
    return ScanSignal(
        kind="review_nag.mr_state_unreadable",
        summary=f"Skipping nag for {post.mr_url} — {why}",
        payload={"mr_url": post.mr_url, "post_id": post.pk, "reason": why},
    )


def _consult_guard_before_nag(post: ReviewRequestPost, overlay_name: str) -> ScanSignal | None:
    """Live-read dedup before nagging (#1084).

    If the review was already requested again / picked up out-of-band (a user
    or another actor re-posted the MR URL in the channel window), reconcile the
    row (``done_at`` set, PR transitioned) and skip the nag so the train stops.
    Only a confirmed clean read permits the nag. Missing transport or an
    unreadable history refuses this tick without consuming the nag claim.
    """
    from teatree.core.gates.review_request_guard import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
        ReconcileStatus,
        reconcile_out_of_band,
        resolve_guard_target,
    )

    target = resolve_guard_target(channel_id=post.slack_channel_id, overlay_name=overlay_name)
    if target is None:
        return ScanSignal(
            kind="review_nag.guard_unreadable",
            summary=f"Review channel for {post.mr_url} is unreadable — holding the nag",
            payload={"mr_url": post.mr_url, "post_id": post.pk},
        )
    # The row's own review-request root carries this URL too, so counting it would
    # reconcile the row on its first due tick — retiring the nag, the `:merge:`
    # reaction and the resume reply, all three of which read `done_at`.
    result = reconcile_out_of_band(
        mr_url=post.mr_url,
        target=target,
        overlay=overlay_name,
        ignore_ts=post.slack_thread_ts,
    )
    if result.status is ReconcileStatus.ABSENT:
        return None
    if result.status is ReconcileStatus.UNREADABLE:
        return ScanSignal(
            kind="review_nag.guard_unreadable",
            summary=f"Review channel history for {post.mr_url} is unreadable — holding the nag",
            payload={"mr_url": post.mr_url, "post_id": post.pk},
        )
    return ScanSignal(
        kind="review_nag.reconciled",
        summary=f"Review for {post.mr_url} already requested out-of-band — nag train stopped",
        payload={"mr_url": post.mr_url, "permalink": result.permalink, "post_id": post.pk},
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
    # ``approvals_left`` — not ``approved_by`` — is the field both backends populate
    # correctly: GitHub hard-codes ``approved_by=[]`` unconditionally (#8), so reading its
    # truthiness misreads every GitHub-backed approval as NOT_APPROVED and nags forever.
    return ApprovalReadState.APPROVED if state["approvals_left"] <= 0 else ApprovalReadState.NOT_APPROVED
