"""Race-safe review-request dedup against LIVE Slack messages (#1084).

The incident this closes: the agent's xoxp Connect-channel review-request
post for an MR was slow; the user manually posted the same request in the
same channel; the agent then posted a duplicate the user had to delete.

The guard runs in the SAME turn as the post and is the single authority
on whether a review-request message may go out.

Live read, not just the DB. It reads the target channel's recent
``conversations.history`` (recency-bounded via ``oldest``) with the
*same* token the post will use — read-token == post-token, so a
Slack-Connect channel the bot token cannot read is read with the user's
``xoxp`` exactly when the post would use it. ANY in-window message
containing the canonical MR URL suppresses the post, regardless of
author — a user's manual post must suppress the agent.

Atomic DB claim (post path only). A clean "nothing found" read is not
sufficient on its own (two callers could both read empty concurrently).
Before returning POST :func:`should_post_review_request` takes the
``ReviewRequestPost`` ``get_or_create`` claim on ``mr_url``;
``created=False`` SUPPRESSES — but ONLY for a genuine *recent*
concurrent claim. A stale unposted orphan (older than
:data:`_CLAIM_RACE_WINDOW`) is reclaimed → POST, because the live scan
is the authority that nothing was posted (#1103). The decision-only
:func:`peek_should_post_review_request` (used by
``review_request_check``) takes NO claim at all, so it can never leave
an orphan that wedges a later real post.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from django.db import transaction
from django.utils import timezone

from teatree.core.models import PullRequest, ReviewRequestPost

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

_DEFAULT_RECENCY_WINDOW = dt.timedelta(hours=24)
_DEFAULT_READ_TIMEOUT = 8.0
_MAX_PAGES = 5

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
    recency_window: dt.timedelta = _DEFAULT_RECENCY_WINDOW
    read_timeout: float = _DEFAULT_READ_TIMEOUT
    now: dt.datetime | None = None


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
        max_pages=_MAX_PAGES,
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


def _claim_or_reclaim(canonical: str, target: GuardTarget, *, using: str | None = None) -> GuardDecision:
    """Take the durable race-backstop claim, reclaiming a stale orphan.

    A fresh ``get_or_create`` is POST. An existing row is SUPPRESS
    (``already_claimed``) ONLY when it is a genuine *recent* concurrent
    claim. An unposted orphan (``done_at`` unset, no ``slack_thread_ts``)
    older than :data:`_CLAIM_RACE_WINDOW` is stale — the live scan above
    is authoritative that nothing was posted, so it is reclaimed → POST
    (#1103). ``select_for_update`` is a documented SQLite no-op / real
    Postgres lock — kept, matching ``OnBehalfApproval.consume`` (#1098).

    ``using`` selects an alternate Django database alias for the whole
    claim transaction — used by the concurrent regression test to point
    the claim at a file-backed SQLite registered with prod's
    ``transaction_mode=IMMEDIATE`` ``OPTIONS``. Production callers pass no
    ``using`` and run against the default connection.
    """
    manager = ReviewRequestPost.objects.using(using) if using else ReviewRequestPost.objects
    with transaction.atomic(using=using):
        post, created = manager.select_for_update().get_or_create(
            mr_url=canonical,
            defaults={"slack_channel_id": target.channel_id, "slack_thread_ts": ""},
        )
        if created:
            return GuardDecision(action="post")
        is_unposted_orphan = post.done_at is None and not post.slack_thread_ts
        is_stale = timezone.now() - post.created_at > _CLAIM_RACE_WINDOW
        if is_unposted_orphan and is_stale:
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
    opts = options or GuardOptions()
    canonical = _canonical(mr_url)
    terminal = _live_decision(canonical, target, opts)
    if terminal is not None:
        return terminal
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
    opts = options or GuardOptions()
    canonical = _canonical(mr_url)
    terminal = _live_decision(canonical, target, opts)
    return terminal if terminal is not None else GuardDecision(action="post")


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
    opts = options or GuardOptions()
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


def resolve_guard_target(channel_id: str = "", channel_name: str = "") -> GuardTarget | None:
    """Resolve the review channel and the post-token for the active overlay.

    The token is the one an outbound post to the review channel would use:
    a Slack-Connect channel resolves to the user ``xoxp`` via
    :meth:`SlackBotBackend.resolve_channel_token` (read-token ==
    post-token). Falls back to the sync token when the messaging backend
    is not a bot-backed Slack instance. Returns ``None`` when no review
    channel / token is configured (the caller treats that as "cannot
    dedup live → fall back to the DB-only behaviour").
    """
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415 — deferred: call-time import
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: call-time import, kept lazy

    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None
    if not channel_id or not channel_name:
        channel_name, channel_id = overlay.config.get_review_channel()
    if not channel_id:
        return None

    token = _channel_token(messaging_from_overlay(), channel_id, overlay)
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
