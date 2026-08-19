"""Surface a ``pr_sweep`` skip that has persisted across N consecutive passes.

The sweep's skip reasons are each a sound per-tick decision and each log-only, so a
PR skipped every tick forever produces no signal — a finished branch quietly never
merges and nobody is told why. Silence is the defect, not the skip.

This reads the sweep's own emitted signals rather than reaching into the scanner, so
``pr_sweep`` needs no hook: a ``pr_sweep.skip`` extends that PR's streak, any other
``pr_sweep.*`` outcome resolves it, and a streak reaching :data:`SURFACE_AFTER_TICKS`
is announced — then again only after backing off for :data:`REANNOUNCE_COOLDOWN`.
A granular reason wobble (``ci_red`` flapping to ``ci_pending`` and back on the same
stuck PR) is not a new problem, and the ledger reads it as one condition on both sides:
the streak keeps counting through it (the CI-verdict reasons are one group, so the
flappiest PRs still reach the threshold rather than restarting below it every tick), and
the backoff is reason-independent on BOTH halves — the DB gate and the idempotency key
(see :func:`_announcement_key`) — so the wobble never re-arms an immediate second DM.
Only the cooldown window does. ``t3 doctor check`` reports every aged streak standing,
announced or not.

Every failure is swallowed: a missing table, an unreachable DM transport, or a
malformed payload must degrade to a quiet tick, never abort one.
"""

import datetime as dt
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import SweepSkipStreak

logger = logging.getLogger(__name__)

#: Consecutive identical skips before a PR is announced. Below this a skip is
#: ordinary sweep bookkeeping; at it, the PR is stuck and nobody has been told.
SURFACE_AFTER_TICKS = 3

#: Minimum time between announcements for the SAME PR, regardless of how many times
#: (or how often) its skip reason changes in between. Stops a flapping reason from
#: re-arming a fresh DM every few ticks while still reminding the owner daily that a
#: PR is genuinely still stuck.
REANNOUNCE_COOLDOWN = dt.timedelta(hours=24)

#: How long a tracked PR may go unobserved before the pass reads it as gone from the open
#: set. ``PrSweepScanner.scan`` emits no signal for a PR whose evaluation raised, so one
#: missed pass is not a departure; at the ~5-min sweep cadence this is ~12 passes of slack.
DEPARTURE_GRACE = dt.timedelta(hours=1)

_SKIP_KIND = "pr_sweep.skip"
_SWEEP_PREFIX = "pr_sweep."

type SkipNotifier = Callable[..., None]


def _default_notify(*, text: str, idempotency_key: str) -> None:
    from teatree.core.modelkit.notify_policy import NotifyAudience  # noqa: PLC0415 — deferred: integration import
    from teatree.core.notify import NotifyKind  # noqa: PLC0415 — deferred: integration import
    from teatree.messaging import notify_with_fallback  # noqa: PLC0415 — deferred: integration import

    notify_with_fallback(
        text=text,
        kind=NotifyKind.INFO,
        idempotency_key=idempotency_key,
        audience=NotifyAudience.OWNER_ESCALATION,
    )


def _surface_text(row: "SweepSkipStreak") -> str:
    return (
        f"PR {row.ref} has been skipped by the merge sweep {row.tick_count} consecutive times "
        f"({row.age_label()}) — reason `{row.reason}`. {row.link}"
    )


def _observe(signal: ScanSignal, moment: dt.datetime) -> None:
    from teatree.core.models import SkipObservation, SweepSkipStreak  # noqa: PLC0415 — deferred: ORM app registry

    payload = signal.payload
    slug = str(payload.get("slug") or "")
    pr_id = payload.get("pr_id")
    if not slug or not isinstance(pr_id, int):
        return
    if signal.kind != _SKIP_KIND:
        SweepSkipStreak.objects.resolve(slug=slug, pr_id=pr_id)
        return
    SweepSkipStreak.objects.observe(
        SkipObservation(
            slug=slug,
            pr_id=pr_id,
            reason=str(payload.get("reason") or "unknown"),
            url=str(payload.get("url") or ""),
            overlay=str(payload.get("overlay") or ""),
        ),
        now=moment,
    )


def _settled_refs() -> set[tuple[str, int]]:
    """Every tracked PR a local row proves MERGED or CLOSED — one query, no forge call.

    Absence of a row is UNKNOWN, never dead: most tracked PRs are opened outside the
    pipeline and carry no row at all, so requiring one to announce would mute the
    majority of real alarms. Newest row wins per PR, the rule
    :meth:`~teatree.core.models.pull_request.PullRequestQuerySet.owning_ticket` states.
    """
    from teatree.core.models import PullRequest, SweepSkipStreak  # noqa: PLC0415 — deferred: ORM app registry

    tracked = {str(pr_id) for pr_id in SweepSkipStreak.objects.values_list("pr_id", flat=True)}
    if not tracked:
        return set()
    rows = PullRequest.objects.filter(iid__in=tracked).order_by("id").values_list("repo", "iid", "state")
    newest = {(repo.casefold(), int(iid)): state for repo, iid, state in rows if iid.isdigit()}
    terminal = {PullRequest.State.MERGED, PullRequest.State.CLOSED}
    return {ref for ref, state in newest.items() if state in terminal}


def _swept_slugs(signals: list[ScanSignal]) -> set[str]:
    return {slug for signal in signals if (slug := str(signal.payload.get("slug") or ""))}


def _discard_fossils(signals: list[ScanSignal], moment: dt.datetime) -> None:
    """Drop the streaks of PRs the sweep can no longer merge, by either local proof (#4518).

    A closed PR leaves ``list_open_prs``, so ``resolve`` — which fires only on a live
    ``pr_sweep.*`` signal — never runs and the row outlives the PR it describes.
    """
    from teatree.core.models import SweepSkipStreak  # noqa: PLC0415 — deferred: ORM app registry

    SweepSkipStreak.objects.drop_terminal(terminal_refs=_settled_refs())
    SweepSkipStreak.objects.drop_departed(
        slugs=_swept_slugs(signals),
        stale_before=moment - DEPARTURE_GRACE,
    )


def _announcement_key(row: "SweepSkipStreak", *, moment: dt.datetime, cooldown: dt.timedelta) -> str:
    """One key per PR per cooldown window — deliberately NOT per reason.

    The notify ledger no-ops a key it has already delivered, forever. Keying on the
    reason therefore swallowed the backed-off reminder in the case that matters most
    (a PR stuck on ONE unchanged reason for days) while a reason wobble sailed
    through — the exact inversion of the backoff. A window index is stable for a
    re-run inside the window and provably distinct once ``cooldown`` has elapsed.

    A non-positive ``cooldown`` degrades to a one-second window rather than raising:
    this module's contract is that no failure aborts a tick.
    """
    window = int(moment.timestamp() // max(cooldown.total_seconds(), 1.0))
    return f"pr_sweep_aged_skip:{row.ref}:{window}"


def _announce_due(
    notify: SkipNotifier,
    threshold: int,
    cooldown: dt.timedelta,
    moment: dt.datetime,
) -> list[str]:
    from teatree.core.models import SweepSkipStreak  # noqa: PLC0415 — deferred: ORM import needs the app registry

    announced: list[str] = []
    due = list(
        SweepSkipStreak.objects.due_to_surface(
            threshold=threshold,
            cooldown=cooldown,
            now=moment,
            observed_since=moment,
        ),
    )
    for row in due:
        try:
            notify(text=_surface_text(row), idempotency_key=_announcement_key(row, moment=moment, cooldown=cooldown))
        except Exception:
            logger.exception("pr_sweep aged-skip surfacing failed to notify for %s", row.ref)
        announced.append(row.ref)
    # Stamped regardless of delivery: an undeliverable DM must not re-fire the same
    # announcement every tick — the doctor view keeps reporting it standing.
    SweepSkipStreak.objects.mark_surfaced([row.pk for row in due], now=moment)
    return announced


def record_sweep_outcomes(
    signals: Iterable[ScanSignal],
    *,
    notify: SkipNotifier | None = None,
    threshold: int = SURFACE_AFTER_TICKS,
    cooldown: dt.timedelta = REANNOUNCE_COOLDOWN,
    now: dt.datetime | None = None,
) -> list[str]:
    """Fold this tick's sweep signals into the streak ledger; announce the newly aged.

    Returns the refs announced this tick (empty on the common path).
    """
    sweep_signals = [signal for signal in signals if signal.kind.startswith(_SWEEP_PREFIX)]
    if not sweep_signals:
        return []
    try:
        moment = now or timezone.now()
        for signal in sweep_signals:
            _observe(signal, moment)
        _discard_fossils(sweep_signals, moment)
        return _announce_due(notify or _default_notify, threshold, cooldown, moment)
    except Exception:
        logger.exception("pr_sweep aged-skip surfacing failed")
        return []
