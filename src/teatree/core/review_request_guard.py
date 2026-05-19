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

Atomic DB claim. A clean "nothing found" read is not sufficient on its
own (two callers could both read empty concurrently). Before returning
POST the guard takes the existing ``ReviewRequestPost`` ``get_or_create``
claim on ``mr_url``; ``created=False`` means a concurrent caller already
claimed it, so SUPPRESS.

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

import httpx
from django.db import transaction
from django.utils import timezone

from teatree.backends.slack import SlackReviewSearchRequest, read_recent_review_matches
from teatree.core.models import PullRequest, ReviewRequestPost

logger = logging.getLogger(__name__)

_DEFAULT_RECENCY_WINDOW = dt.timedelta(hours=24)
_DEFAULT_READ_TIMEOUT = 8.0
_MAX_PAGES = 5


def _canonical(url: str) -> str:
    """Canonicalize an MR/PR URL (mirrors ``slack._iter_review_matches``)."""
    return url.rstrip("/").split("#")[0]


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
    right_now = opts.now or timezone.now()
    oldest = right_now - opts.recency_window
    request = SlackReviewSearchRequest(
        token=target.token,
        channel_id=target.channel_id,
        channel_name=target.channel_name,
        pr_urls=[canonical],
        max_pages=_MAX_PAGES,
        oldest_ts=f"{oldest.timestamp():.6f}",
        timeout=opts.read_timeout,
    )
    try:
        read = read_recent_review_matches(request)
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning("review_request_guard: live read failed for %s: %s", canonical, exc)
        return False, []
    if not read.ok:
        return False, []
    oldest_epoch = oldest.timestamp()
    in_window = [m for m in read.matches if m.pr_url == canonical and m.ts and _ts_epoch(m.ts) >= oldest_epoch]
    return True, in_window


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
    invariant.
    """
    opts = options or GuardOptions()
    canonical = _canonical(mr_url)

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

    with transaction.atomic():
        _, created = ReviewRequestPost.objects.get_or_create(
            mr_url=canonical,
            defaults={"slack_channel_id": target.channel_id, "slack_thread_ts": ""},
        )
    if not created:
        return GuardDecision(action="suppress", reason="already_claimed")
    return GuardDecision(action="post")


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
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

    from teatree.backends.slack_bot import SlackBotBackend  # noqa: PLC0415
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415
    from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

    try:
        overlay = get_overlay()
    except ImproperlyConfigured:
        return None
    if not channel_id or not channel_name:
        channel_name, channel_id = overlay.config.get_review_channel()
    if not channel_id:
        return None

    messaging = messaging_from_overlay()
    if isinstance(messaging, SlackBotBackend):
        token = messaging.resolve_channel_token(channel_id)
    else:
        token = overlay.config.get_slack_token()
    if not token:
        return None
    return GuardTarget(channel_id=channel_id, channel_name=channel_name, token=token)
