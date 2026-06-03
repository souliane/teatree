"""Fibonacci nag scanner for unreviewed MRs in the review channel (#1038).

The user posts MRs to the overlay's review channel; the bot tracks each
post in a ``ReviewRequestPost`` row. This scanner walks those rows on every tick
and, when an MR is still unreviewed, posts a thread reply nagging the
``@engineers`` user group at +1, +2, +3, and +5 days. At +5 days with no
pickup, the scanner DMs the user a long-stale warning and marks the row
done so the nag train stops.

Idempotency lives on ``ReviewRequestPost.last_nag_step`` — the row's
fibonacci index advances at most once per nag. A scanner re-run in the
same day window is a no-op because the step has already been recorded.

Backfill safety: rows older than 5 days with ``last_nag_step == 0`` are
treated as historic (the model was added after the post was made) and
marked done without posting; we never know which nags should have fired.

Slack-Connect failure: ``post_message`` against a channel the bot isn't
in raises ``not_in_channel``. The scanner catches the exception, surfaces
it as a ``review_nag.post_failed`` signal, and leaves the row alone so a
future re-invitation can let the nag finally land.

Disabled by default: the scanner only runs when ``review_nag_enabled`` is
``true`` (global or per-overlay). It ships OFF after a concurrent-tick race
double-posted bumps into the colleague review channel (see below).

Concurrency: two loop ticks running against the same row both read the
same ``last_nag_step`` and would each post. The nag is claimed with an
atomic conditional ``UPDATE`` (``last_nag_step`` advanced only if it still
equals the value this tick observed) *before* the Slack post — the tick
that loses the claim skips silently, so exactly one nag is posted per
fibonacci window even under concurrency.

Merged/closed safety: before posting, the MR's open-state is checked via
the code-host backend. A MERGED MR is routed through ``react_merge_on_post``
so the ``:merge:`` reaction still lands (and the row is closed) when the nag
scanner reaches it before the merge-react scanner — the shared ``done_at``
claim keeps it to one reaction across both paths. A CLOSED MR is just marked
done (``review_nag.mr_closed``). An ``UNKNOWN`` state (no backend, auth/network
failure, unparsable URL) fails open and the nag proceeds as before.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field

from django.utils import timezone

from teatree.backends.protocols import CodeHostBackend, MessagingBackend, PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.review_request_merge_react import react_merge_on_post

logger = logging.getLogger(__name__)


# Fibonacci days at which an unreviewed MR gets a nag. Index 0 in the
# sequence is the original post (step 0); index N is the Nth bump.
_FIBONACCI_DAYS: tuple[int, ...] = (1, 2, 3, 5)
_TERMINAL_STEP: int = len(_FIBONACCI_DAYS)  # 4 — "DM user and stop".


def fibonacci_step_for_age(age: dt.timedelta) -> int:
    """Map the age of a ``ReviewRequestPost`` to the highest fibonacci step it's reached.

    Returns ``0`` for an age below the first nag threshold (under one day),
    ``len(_FIBONACCI_DAYS)`` once the age has crossed the terminal +5d
    boundary. Between thresholds the step is the index of the largest
    fibonacci day that the age has passed — so +1.5d → 1, +4d → 3, +5.1d → 4.
    """
    days = age.total_seconds() / 86_400.0
    step = 0
    for index, threshold in enumerate(_FIBONACCI_DAYS, start=1):
        if days >= threshold:
            step = index
    return step


@dataclass(slots=True)
class ReviewNagScanner:
    """Walk ``ReviewRequestPost`` rows and post the next fibonacci nag.

    Stateless beyond the DB rows it walks. Safe to invoke from every loop
    tick — at most one Slack post per row per fibonacci window, enforced
    by the ``last_nag_step`` column.
    """

    messaging: MessagingBackend | None
    user_slack_id: str
    host: CodeHostBackend | None = None
    identities: tuple[str, ...] = field(default_factory=tuple)
    now: dt.datetime | None = None
    name: str = "review_nag"

    def scan(self) -> list[ScanSignal]:
        from teatree.config import load_config  # noqa: PLC0415

        if not load_config().user.review_nag_enabled:
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
        age = right_now - post.created_at
        target_step = fibonacci_step_for_age(age)

        # Backfill: row older than +5d but never recorded a step → historic.
        # Never spam a nag for a post the bot didn't track at creation.
        if target_step == _TERMINAL_STEP and post.last_nag_step == 0:
            post.last_nag_step = _TERMINAL_STEP
            post.done_at = right_now
            post.save(update_fields=["last_nag_step", "done_at"])
            return ScanSignal(
                kind="review_nag.backfill_skip",
                summary=f"Historic review-request post for {post.mr_url} marked done",
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )

        # Already past terminal step → DM the user once and mark done.
        if target_step >= _TERMINAL_STEP and post.last_nag_step >= _TERMINAL_STEP:
            return self._dm_user_and_close(post, messaging, right_now)

        # No new step to bump to.
        if target_step <= post.last_nag_step:
            return None

        # Never nag a merged/closed MR — mark done and skip the post.
        closed = self._close_if_mr_not_open(post, messaging, right_now)
        if closed is not None:
            return closed

        return _post_thread_nag(post, messaging, target_step)

    def _close_if_mr_not_open(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
    ) -> ScanSignal | None:
        """Resolve the row when the MR is merged/closed (no nag posted).

        A MERGED MR is handed to :func:`react_merge_on_post` so the
        ``:merge:`` reaction still lands when the nag scanner reaches the
        row before the merge-react scanner does — the shared claim keeps it
        to exactly one reaction across both paths. A CLOSED (not merged) MR
        just marks the row done. Fails open: no code-host backend, or an
        ``UNKNOWN`` open-state (auth/network failure, unparsable URL),
        returns ``None`` and the nag proceeds — the guard must never wedge
        the train on an unverifiable state.
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
        if open_state is not PrOpenState.CLOSED:
            return None
        post.done_at = right_now
        post.save(update_fields=["done_at"])
        return ScanSignal(
            kind="review_nag.mr_closed",
            summary=f"Review-request post for {post.mr_url} closed — MR is {open_state.value}",
            payload={"mr_url": post.mr_url, "post_id": post.pk, "open_state": open_state.value},
        )

    def _dm_user_and_close(
        self,
        post: ReviewRequestPost,
        messaging: MessagingBackend,
        right_now: dt.datetime,
    ) -> ScanSignal:
        if not self.user_slack_id:
            post.done_at = right_now
            post.save(update_fields=["done_at"])
            return ScanSignal(
                kind="review_nag.stale_no_dm",
                summary=f"Long-stale MR {post.mr_url} closed without DM (no user_slack_id)",
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )

        text = (
            f":information_source: *long-stale MR* — no reviewer for {post.mr_url} "
            "after 5 days of fibonacci nags. Manual escalation recommended."
        )
        dm_delivered = False
        try:
            dm_channel = messaging.open_dm(self.user_slack_id)
            messaging.post_message(channel=dm_channel, text=text, thread_ts="")
            dm_delivered = True
        except Exception as exc:  # noqa: BLE001 — DM transport must never crash a tick.
            logger.warning(
                "review_nag: stale-DM failed for %s to %s: %s",
                post.mr_url,
                self.user_slack_id,
                exc,
            )
        if not dm_delivered:
            return ScanSignal(
                kind="review_nag.stale_no_dm",
                summary=f"Stale-DM failed for {post.mr_url} — nag train left open for retry",
                payload={"mr_url": post.mr_url, "post_id": post.pk},
            )
        post.done_at = right_now
        post.save(update_fields=["done_at"])
        return ScanSignal(
            kind="review_nag.stale_dm",
            summary=f"DM'd user about long-stale MR {post.mr_url}",
            payload={"mr_url": post.mr_url, "post_id": post.pk},
        )


def _consult_guard_before_nag(post: ReviewRequestPost) -> ScanSignal | None:
    """Live-read dedup before nagging (#1084).

    If the review was already requested again / picked up out-of-band
    (the user or another actor posted in-window), reconcile the row
    (``done_at`` set, PR transitioned) and skip the nag so the train
    stops. Fails open: a missing channel/token or a slow/failed read
    returns ``None`` and the nag proceeds as before — the guard must
    never wedge the loop on a Slack read.
    """
    from teatree.core.review_request_guard import reconcile_out_of_band, resolve_guard_target  # noqa: PLC0415

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


def _post_thread_nag(
    post: ReviewRequestPost,
    messaging: MessagingBackend,
    target_step: int,
) -> ScanSignal | None:
    reconciled = _consult_guard_before_nag(post)
    if reconciled is not None:
        return reconciled

    # Atomic claim BEFORE posting: advance ``last_nag_step`` only if it
    # still equals the value this tick observed. The single winning tick
    # gets ``updated == 1`` and posts; a concurrent tick that already
    # claimed this step gets ``0`` and skips silently — exactly one nag
    # per fibonacci window even under concurrency. Lock-free (no
    # ``select_for_update``); the conditional ``UPDATE`` is the lock.
    claimed_from = post.last_nag_step
    updated = ReviewRequestPost.objects.filter(pk=post.pk, last_nag_step=claimed_from).update(
        last_nag_step=target_step,
    )
    if updated != 1:
        return None

    day_number = _FIBONACCI_DAYS[target_step - 1]
    text = _nag_text(messaging, post.mr_url, day_number)
    try:
        messaging.post_message(
            channel=post.slack_channel_id,
            text=text,
            thread_ts=post.slack_thread_ts,
        )
    except Exception as exc:  # noqa: BLE001 — Slack-Connect not_in_channel etc.
        # Release the claim so a future re-invitation retries the post.
        ReviewRequestPost.objects.filter(pk=post.pk, last_nag_step=target_step).update(
            last_nag_step=claimed_from,
        )
        logger.warning(
            "review_nag: post failed for %s on %s/%s: %s",
            post.mr_url,
            post.slack_channel_id,
            post.slack_thread_ts,
            exc,
        )
        return ScanSignal(
            kind="review_nag.post_failed",
            summary=f"Slack post failed for {post.mr_url}: {exc}",
            payload={"mr_url": post.mr_url, "error": str(exc), "post_id": post.pk},
        )

    post.last_nag_step = target_step
    return ScanSignal(
        kind="review_nag.ping",
        summary=f"Pinged @engineers for {post.mr_url} (day {day_number} of 5)",
        payload={"mr_url": post.mr_url, "step": target_step, "post_id": post.pk},
    )


def _nag_text(messaging: MessagingBackend, mr_url: str, day_number: int) -> str:
    mention = _engineers_mention(messaging)
    return f"{mention} still no reviewer for {mr_url} — bumping (day {day_number} of 5)."


def _engineers_mention(messaging: MessagingBackend) -> str:
    try:
        usergroup_id = messaging.resolve_user_id("engineers")
    except Exception:  # noqa: BLE001 — never crash on a lookup failure.
        usergroup_id = ""
    if usergroup_id:
        return f"<!subteam^{usergroup_id}>"
    return "@engineers"
