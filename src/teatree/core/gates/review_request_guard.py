"""Race-safe review-request dedup against LIVE Slack messages (#1084).

The incident this closes: the agent's xoxp Connect-channel review-request
post for an MR was slow; the user manually posted the same request in the
same channel; the agent then posted a duplicate the user had to delete.

The guard runs in the SAME turn as the post and is the single authority
on whether a review-request message may go out.

Live read, not just the DB. It reads the target channel's recent
``conversations.history`` bounded to ``review_request_dedup_window_days``
(default 30, config-driven — no more hard-coded 24h) with the *same*
token the post will use — read-token == post-token, so a Slack-Connect
channel the bot token cannot read is read with the user's ``xoxp``
exactly when the post would use it. ANY in-window message containing the
canonical MR URL suppresses the post, regardless of author — a user's
manual post must suppress the agent.

Live is the source of truth, never a bare DB row. A posted
``ReviewRequestPost`` row (``slack_thread_ts`` set) is NOT trusted on its
own beyond the window (:func:`_posted_row_terminal`): its exact thread is
live-read via ``conversations.replies`` (routed token) — still there ⇒
SUPPRESS + refresh; gone (deleted) ⇒ reclaim → POST. Both ``post`` and
``check`` (peek) share this verification so they agree beyond the window.

Atomic DB claim (post path only). A clean "nothing found" read is not
sufficient on its own (two callers could both read empty concurrently).
When no posted row survives verification, :func:`should_post_review_request`
takes the ``ReviewRequestPost`` ``get_or_create`` claim on ``mr_url``;
``created=False`` SUPPRESSES — but ONLY for a genuine *recent* concurrent
unposted claim. A stale unposted orphan (older than
:data:`_CLAIM_RACE_WINDOW`) is reclaimed → POST, because the live scan is
the authority that nothing was posted (#1103). The decision-only
:func:`peek_should_post_review_request` (used by ``review_request_check``)
takes NO claim at all, so it can never leave an orphan that wedges a later
real post.

Fail safe. The httpx read is bounded (hard timeout + bounded pages). On
timeout / HTTP error / API not-ok the guard SUPPRESSES with
``reason="read_failed_failsafe"`` — biasing to *not* posting a possible
duplicate. The obligation stays open for a later tick (no row is
written, the loop will retry).

Reconciliation (guard-owned signals only). When an out-of-band / user /
prior post is detected, ``ReviewRequestPost.done_at`` is set so
``ReviewNagScanner`` stops nagging, and the matching ``PullRequest``
transitions OPEN to REVIEW_REQUESTED with the discovered permalink. This
deliberately touches ONLY ``ReviewRequestPost`` and ``PullRequest`` —
never a loop ``Task`` row or ``teatree.loop.mechanical`` task completion
(that lifecycle is owned by souliane/teatree#1086 / #1074 / #1077, in
independent review).
"""

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from django.db import transaction
from django.utils import timezone

from teatree.core.models import PullRequest, ReviewRequestPost
from teatree.core.overlay_loader import infer_overlay_for_url

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

_DEFAULT_READ_TIMEOUT = 8.0
# Static fallback page cap; the live default is config-driven — see
# :func:`_default_options` (``review_request_dedup_max_pages``, default 5).
_MAX_PAGES = 5
# Static fallback only; the live default window is config-driven — see
# :func:`_default_options` (``review_request_dedup_window_days``, default 30).
_FALLBACK_RECENCY_WINDOW = dt.timedelta(days=30)

# A durable claim is the concurrent check→post race backstop only. An
# unposted claim older than this window is a stale orphan (e.g. the
# decision-only ``review_request_check`` command never posts), not a
# concurrent dup — it must not override an authoritative live-scan
# "not posted" (#1103). Only a *recent* unposted claim is a genuine race.
_CLAIM_RACE_WINDOW = dt.timedelta(seconds=120)


def _canonical(url: str) -> str:
    """Canonicalize an MR/PR URL (mirrors ``slack._iter_review_matches``)."""
    return url.rstrip("/").split("#")[0]


# Public alias so the #1098 sanctioned-post command uses the exact same
# canonicalization for BOTH the guard arg and the #960 approval target —
# provably one string, no drift between the dedup claim and the approval scope.
canonical_mr_url = _canonical


@dataclass(frozen=True, slots=True)
class GuardTarget:
    """The review channel and the token an outbound post would use.

    The token is load-bearing: it MUST equal the token the post will use
    (Connect-aware via :func:`resolve_guard_target`) so the live history
    read sees what the post would write — read-token == post-token.
    """

    channel_id: str
    channel_name: str
    token: str


@dataclass(frozen=True, slots=True)
class GuardDecision:
    action: str  # "post" | "suppress"
    permalink: str = ""
    author: str = ""
    reason: str = ""

    @property
    def should_post(self) -> bool:
        return self.action == "post"


def _reconcile(mr_url: str, permalink: str) -> None:
    """Mark the obligation satisfied without touching the loop Task lifecycle.

    Sets ``ReviewRequestPost.done_at`` (so ``ReviewNagScanner`` —
    ``done_at__isnull=True`` filter — stops nagging) and transitions any
    matching ``PullRequest`` OPEN → REVIEW_REQUESTED. Idempotent: a row
    already done / a PR already past OPEN is left as-is.
    """
    with transaction.atomic():
        post, _ = ReviewRequestPost.objects.get_or_create(
            mr_url=mr_url,
            defaults={"slack_channel_id": "", "slack_thread_ts": ""},
        )
        if post.done_at is None:
            post.done_at = timezone.now()
            post.save(update_fields=["done_at"])
        for pr in PullRequest.objects.filter(url=mr_url, state=PullRequest.State.OPEN):
            pr.request_review(slack_url=permalink)
            pr.save()


@dataclass(frozen=True, slots=True)
class GuardOptions:
    recency_window: dt.timedelta = _FALLBACK_RECENCY_WINDOW
    read_timeout: float = _DEFAULT_READ_TIMEOUT
    now: dt.datetime | None = None
    max_pages: int = _MAX_PAGES


def _default_options() -> GuardOptions:
    """Build guard options with the config-driven live-Slack dedup window (#1084 follow-up).

    ``review_request_dedup_window_days`` (default 30) replaces the old
    hard-coded 24h — so live Slack, not the DB row's age, decides.
    ``review_request_dedup_max_pages`` (default 5) makes the channel-scan page
    cap configurable so a ~30-day window is actually reachable on a busy
    channel (#3292 part 4).
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: Django settings at call time

    settings = get_effective_settings()
    return GuardOptions(
        recency_window=dt.timedelta(days=settings.review_request_dedup_window_days),
        max_pages=settings.review_request_dedup_max_pages,
    )


def _live_matches(
    canonical: str,
    target: GuardTarget,
    opts: GuardOptions,
) -> tuple[bool, list]:
    """Recency-bounded live read. Returns ``(ok, in_window_matches)``.

    ``ok`` is False on any failed/timed-out/not-ok read so the caller
    fails safe to suppression.
    """
    from teatree.core.backend_registry import ReviewSearchSpec, get_backend_provider  # noqa: PLC0415 — lazy import

    right_now = opts.now or timezone.now()
    oldest = right_now - opts.recency_window
    spec = ReviewSearchSpec(
        token=target.token,
        channel_id=target.channel_id,
        channel_name=target.channel_name,
        pr_urls=[canonical],
        max_pages=opts.max_pages,
        oldest_ts=f"{oldest.timestamp():.6f}",
        timeout=opts.read_timeout,
    )
    try:
        read = get_backend_provider().read_recent_review_matches(spec)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("review_request_guard: live read failed for %s: %s", canonical, exc)
        return False, []
    if not read.ok:
        return False, []
    oldest_epoch = oldest.timestamp()
    in_window = [m for m in read.matches if m.pr_url == canonical and m.ts and _ts_epoch(m.ts) >= oldest_epoch]
    return True, in_window


def _live_decision(
    canonical: str,
    target: GuardTarget,
    opts: GuardOptions,
) -> GuardDecision | None:
    """The live-scan terminal decision shared by check and post (#1103).

    Returns the terminal ``GuardDecision`` when the live read fails
    (``read_failed_failsafe``) or finds an in-window message
    (``already_posted``, after reconciling). Returns ``None`` when the
    live scan succeeded and found NO in-window message — the caller then
    decides whether to take/peek a durable claim. This is the single
    authoritative dedup head; ``check`` (peek, no claim) and ``post``
    (claim) share it without duplicating ``_live_matches``/``_reconcile``.
    """
    ok, in_window = _live_matches(canonical, target, opts)
    if not ok:
        return GuardDecision(action="suppress", reason="read_failed_failsafe")
    if in_window:
        match = in_window[0]
        _reconcile(canonical, match.permalink)
        return GuardDecision(
            action="suppress",
            permalink=match.permalink,
            author=match.author,
            reason="already_posted",
        )
    return None


def _read_thread_activity(target: GuardTarget, thread_ts: str, opts: GuardOptions, *, channel_id: str = ""):  # noqa: ANN202 — provider-owned ThreadActivityReadLike; a return annotation would force a core→backends type import
    """Routed-token ``conversations.replies`` read for one thread; ``None`` on transport failure.

    Reads in *channel_id* when supplied — the channel the post was RECORDED
    under (#3292 part 3) — so a review-channel change since the post does not
    read the wrong channel and mis-decide; falls back to the target channel.
    """
    from teatree.core.backend_registry import ThreadActivitySpec, get_backend_provider  # noqa: PLC0415 — lazy import

    spec = ThreadActivitySpec(
        token=target.token,
        channel_id=channel_id or target.channel_id,
        thread_ts=thread_ts,
        timeout=opts.read_timeout,
    )
    try:
        return get_backend_provider().read_thread_activity(spec)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("review_request_guard: thread read failed for %s/%s: %s", target.channel_id, thread_ts, exc)
        return None


def _posted_row_terminal(
    canonical: str,
    target: GuardTarget,
    opts: GuardOptions,
    *,
    mutate: bool,
) -> GuardDecision | None:
    """Live-verify a POSTED ``ReviewRequestPost`` row before trusting it (#1084 follow-up).

    Kills the "24h DB-decides" behaviour: a row with a ``slack_thread_ts``
    is NOT trusted on its own. The exact thread is live-read with the
    routed post-token — still there ⇒ SUPPRESS (and, when ``mutate``,
    refresh via :func:`_reconcile` so the nag stops); gone ⇒ POST (and,
    when ``mutate``, atomically reclaim the row so a concurrent caller
    can't double-post). Fail-safe: ANY read failure ⇒ SUPPRESS. Returns
    ``None`` when there is no posted row — the caller's claim/POST path
    then runs. ``mutate=False`` is the ``check`` (peek) path: same verdict,
    no DB write.
    """
    post = ReviewRequestPost.objects.filter(mr_url=canonical).first()
    if post is None or not post.slack_thread_ts:
        return None
    read = _read_thread_activity(target, post.slack_thread_ts, opts, channel_id=post.slack_channel_id)
    if read is None or not read.ok:
        return GuardDecision(action="suppress", reason="read_failed_failsafe")
    if read.exists:
        if mutate:
            _reconcile(canonical, "")
        return GuardDecision(action="suppress", reason="already_claimed")
    if not mutate:
        return GuardDecision(action="post", reason="thread_gone")
    if _reclaim_posted_row(canonical, post.slack_thread_ts, target.channel_id):
        return GuardDecision(action="post", reason="thread_gone")
    return GuardDecision(action="suppress", reason="already_claimed")


def _reclaim_posted_row(canonical: str, observed_ts: str, channel_id: str) -> bool:
    """Atomically reset a posted-but-gone row to a fresh unposted claim.

    The conditional ``UPDATE`` (guarded on the exact ``slack_thread_ts``
    this caller observed as gone) is the race backstop: the single winner
    gets ``updated == 1`` and POSTs; a concurrent caller that already reset
    the row gets ``0`` and suppresses — so a deleted-message reclaim can
    never double-post.
    """
    updated = ReviewRequestPost.objects.filter(mr_url=canonical, slack_thread_ts=observed_ts).update(
        slack_thread_ts="",
        slack_channel_id=channel_id,
        created_at=timezone.now(),
        done_at=None,
    )
    return updated == 1


def _claim_or_reclaim(canonical: str, target: GuardTarget, *, using: str | None = None) -> GuardDecision:
    """Take the durable race-backstop claim, reclaiming a stale unposted orphan.

    Reached only after the live channel scan found nothing AND no posted
    row survived live verification (:func:`_posted_row_terminal`). A fresh
    ``get_or_create`` is POST. An existing row here is an *unposted* claim
    (no ``slack_thread_ts``): older than :data:`_CLAIM_RACE_WINDOW` it is a
    stale orphan the live scan proves nothing was posted for → reclaim →
    POST (#1103); recent it is a genuine concurrent claim → SUPPRESS.
    ``select_for_update`` is a documented SQLite no-op / real Postgres lock
    (matching ``OnBehalfApproval.consume``). ``using`` selects an alternate
    DB alias for the concurrent regression test; production passes none.
    """
    manager = ReviewRequestPost.objects.using(using) if using else ReviewRequestPost.objects
    with transaction.atomic(using=using):
        post, created = manager.select_for_update().get_or_create(
            mr_url=canonical,
            defaults={"slack_channel_id": target.channel_id, "slack_thread_ts": ""},
        )
        if created:
            return GuardDecision(action="post")
        is_stale = timezone.now() - post.created_at > _CLAIM_RACE_WINDOW
        if not post.slack_thread_ts and is_stale:
            post.created_at = timezone.now()
            post.slack_channel_id = target.channel_id
            post.save(update_fields=["created_at", "slack_channel_id"])
            return GuardDecision(action="post")
        return GuardDecision(action="suppress", reason="already_claimed")


def should_post_review_request(
    *,
    mr_url: str,
    target: GuardTarget,
    options: GuardOptions | None = None,
) -> GuardDecision:
    """Decide whether a review-request message for *mr_url* may be posted.

    ``target.token`` MUST be the token the post will use (resolved via
    :func:`resolve_guard_target` so a Connect channel uses the user
    ``xoxp``). Read-token == post-token is a load-bearing correctness
    invariant. Takes the durable race-backstop claim — callers that do
    NOT post (the decision-only ``review_request_check``) must use
    :func:`peek_should_post_review_request` instead (#1103).
    """
    opts = options or _default_options()
    canonical = _canonical(mr_url)
    terminal = _live_decision(canonical, target, opts)
    if terminal is not None:
        return terminal
    posted = _posted_row_terminal(canonical, target, opts, mutate=True)
    if posted is not None:
        return posted
    return _claim_or_reclaim(canonical, target)


def peek_should_post_review_request(
    *,
    mr_url: str,
    target: GuardTarget,
    options: GuardOptions | None = None,
) -> GuardDecision:
    """Decision-only variant: same live-scan dedup, NO durable claim (#1103).

    ``review_request_check`` is a pre-post gate that never posts, so it
    must not persist a ``ReviewRequestPost`` claim — a left-behind orphan
    would wedge every subsequent post on ``already_claimed`` even though
    the authoritative live scan confirmed nothing is posted. Returns the
    same :class:`GuardDecision` shape as
    :func:`should_post_review_request` (terminal live-scan decision, or
    ``post`` when the channel is clean) without touching the DB.
    """
    opts = options or _default_options()
    canonical = _canonical(mr_url)
    terminal = _live_decision(canonical, target, opts)
    if terminal is not None:
        return terminal
    posted = _posted_row_terminal(canonical, target, opts, mutate=False)
    return posted if posted is not None else GuardDecision(action="post")


def reconcile_out_of_band(
    *,
    mr_url: str,
    target: GuardTarget,
    options: GuardOptions | None = None,
) -> str:
    """Live-read-only reconciliation (no DB claim) for the nag path (#1084).

    Returns the discovered permalink when an in-window message for
    *mr_url* exists (and reconciles ``done_at`` + the PR transition so the
    nag train stops), else ``""``. A failed/timed-out read returns ``""``
    — the nag still fires; it must never wedge on a slow Slack read.
    """
    opts = options or _default_options()
    canonical = _canonical(mr_url)
    ok, in_window = _live_matches(canonical, target, opts)
    if not ok or not in_window:
        return ""
    match = in_window[0]
    _reconcile(canonical, match.permalink)
    return match.permalink


def _ts_epoch(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


def overlay_for_mr_url(mr_url: str) -> str:
    """The overlay owning *mr_url*, or ``""`` to defer to the ambient default.

    The single precedence rule every review-request surface shares (#1310): an
    explicit ``T3_OVERLAY_NAME`` — what the ``t3 <overlay>`` CLI bridge sets —
    wins and is consumed by :func:`get_overlay`; otherwise the URL's owning
    overlay is inferred from repo ownership. Without it the in-process MCP
    surface (which sets no env var and registers EVERY overlay) resolves no
    overlay at all, and the guard's swallowed ``Multiple overlays found``
    becomes a bogus ``no_review_channel_or_token`` on a perfectly postable
    channel.
    """
    if os.environ.get("T3_OVERLAY_NAME"):
        return ""
    return infer_overlay_for_url(mr_url)


def resolve_guard_target(channel_id: str = "", channel_name: str = "", overlay_name: str = "") -> GuardTarget | None:
    """Resolve the review channel and the post-token for the active overlay.

    The token is the one an outbound post to the review channel would use:
    a Slack-Connect channel resolves to the user ``xoxp`` via
    :meth:`SlackBotBackend.resolve_channel_token` (read-token ==
    post-token). Falls back to the sync token when the messaging backend
    is not a bot-backed Slack instance. Returns ``None`` when no review
    channel / token is configured (the caller treats that as "cannot
    dedup live → fall back to the DB-only behaviour").

    *overlay_name* selects the overlay explicitly, for callers that do not
    run under the CLI's ``T3_OVERLAY_NAME`` bridge — notably the in-process
    MCP server, where every installed overlay is registered and a no-arg
    :func:`get_overlay` raises ``Multiple overlays found``. That raise was
    swallowed into a ``None`` here, degrading into a bogus
    ``no_review_channel_or_token`` (the same mis-routing class as #147).
    Threading the name keeps the MCP and CLI surfaces on one answer.
    """
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415 — deferred: call-time import
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: call-time import, kept lazy

    try:
        overlay = get_overlay(overlay_name or None)
    except ImproperlyConfigured:
        return None
    if not channel_id or not channel_name:
        channel_name, channel_id = overlay.config.get_review_channel()
    if not channel_id:
        return None

    token = _channel_token(messaging_from_overlay(overlay_name or None), channel_id, overlay)
    if not token:
        return None
    return GuardTarget(channel_id=channel_id, channel_name=channel_name, token=token)


def resolve_guard_targets() -> list[GuardTarget]:
    """Resolve every review-broadcast channel + post-token for the active overlay (#1295 cap A).

    Iterates :meth:`OverlayConfig.get_review_broadcast_channels` so a
    Slack-Connect channel uses the per-channel ``xoxp`` from the bot
    backend and a plain channel uses the sync token. Returns an empty
    list when no channels resolve to a usable target — callers fall back
    to the legacy single-channel behaviour.
    """
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415 — deferred: call-time import
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: call-time import, kept lazy

    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return []
    channels = overlay.config.get_review_broadcast_channels()
    if not channels:
        return []
    messaging = messaging_from_overlay()
    targets: list[GuardTarget] = []
    for channel_name, channel_id in channels:
        if not channel_id:
            continue
        token = _channel_token(messaging, channel_id, overlay)
        if not token:
            continue
        targets.append(GuardTarget(channel_id=channel_id, channel_name=channel_name, token=token))
    return targets


def _channel_token(messaging: "MessagingBackend | None", channel_id: str, overlay: "OverlayBase") -> str:
    """Resolve the post token for *channel_id*.

    A Slack bot backend (the one exposing ``resolve_channel_token``) yields a
    per-channel token (the Slack-Connect ``xoxp`` case); any other backend falls
    back to the overlay's configured sync token. Duck-typed on the capability so
    ``core`` does not import the concrete ``SlackBotBackend`` (#1922).
    """
    resolver = getattr(messaging, "resolve_channel_token", None)
    if callable(resolver):
        return resolver(channel_id)
    return overlay.config.get_slack_token()
