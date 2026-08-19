"""Surface a ``pr_sweep`` skip that has persisted across N consecutive passes.

The sweep's skip reasons are each a sound per-tick decision and each log-only, so a
PR skipped every tick forever produces no signal — a finished branch quietly never
merges and nobody is told why. Silence is the defect, not the skip.

This reads the sweep's own emitted signals rather than reaching into the scanner, so
``pr_sweep`` needs no hook: a ``pr_sweep.skip`` extends that PR's streak, any other
per-PR ``pr_sweep.*`` outcome resolves it, and a streak reaching
:data:`SURFACE_AFTER_TICKS` is announced — then again only after backing off for
:data:`REANNOUNCE_COOLDOWN`. A ``pr_sweep.pass`` — one per successfully-listed repo,
naming every PR still open — purges any streak row for that repo whose PR is absent:
gone from the open set (merged or closed) is a finished fact, not a stall. A ``draft``
skip is the one reason that never announces (a deliberate park, not a stall) though it
still accrues and still shows in ``t3 doctor check`` — see
:meth:`teatree.core.models.SweepSkipStreak.objects.due_to_surface`.
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

_SKIP_KIND = "pr_sweep.skip"
_PASS_KIND = "pr_sweep.pass"  # noqa: S105 — a signal-kind name, not a credential
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
        f"({row.age_label()}) — reason `{row.reason}`. {row.url or 'no URL recorded'}"
    )


def _observe(signal: ScanSignal, moment: dt.datetime) -> None:
    from teatree.core.models import SkipObservation, SweepSkipStreak  # noqa: PLC0415 — deferred: ORM app registry

    payload = signal.payload
    slug = str(payload.get("slug") or "")
    if signal.kind == _PASS_KIND:
        if slug:
            seen = [pr_id for pr_id in payload.get("pr_ids") or [] if isinstance(pr_id, int)]
            SweepSkipStreak.objects.purge_absent(slug=slug, seen_pr_ids=seen)
        return
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
    due = list(SweepSkipStreak.objects.due_to_surface(threshold=threshold, cooldown=cooldown, now=moment))
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
        return _announce_due(notify or _default_notify, threshold, cooldown, moment)
    except Exception:
        logger.exception("pr_sweep aged-skip surfacing failed")
        return []
