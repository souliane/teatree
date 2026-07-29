"""Surface a ``pr_sweep`` skip that has persisted across N consecutive passes.

The sweep's skip reasons are each a sound per-tick decision and each log-only, so a
PR skipped every tick forever produces no signal — a finished branch quietly never
merges and nobody is told why. Silence is the defect, not the skip.

This reads the sweep's own emitted signals rather than reaching into the scanner, so
``pr_sweep`` needs no hook: a ``pr_sweep.skip`` extends that PR's streak, any other
``pr_sweep.*`` outcome resolves it, and a streak reaching
:data:`SURFACE_AFTER_TICKS` is announced EXACTLY once (``surfaced_at``). A reason
that changes re-arms one further announcement, because a different reason is a
different problem. ``t3 doctor check`` reports every aged streak standing, announced
or not.

Every failure is swallowed: a missing table, an unreachable DM transport, or a
malformed payload must degrade to a quiet tick, never abort one.
"""

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import SweepSkipStreak

logger = logging.getLogger(__name__)

#: Consecutive identical skips before a PR is announced. Below this a skip is
#: ordinary sweep bookkeeping; at it, the PR is stuck and nobody has been told.
SURFACE_AFTER_TICKS = 3

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
        f"({row.age_label()}) — reason `{row.reason}`. {row.url or 'no URL recorded'}"
    )


def _observe(signal: ScanSignal) -> None:
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
    )


def _announce_due(notify: SkipNotifier, threshold: int) -> list[str]:
    from teatree.core.models import SweepSkipStreak  # noqa: PLC0415 — deferred: ORM import needs the app registry

    announced: list[str] = []
    due = list(SweepSkipStreak.objects.due_to_surface(threshold=threshold))
    for row in due:
        try:
            notify(text=_surface_text(row), idempotency_key=f"pr_sweep_aged_skip:{row.ref}:{row.reason}")
        except Exception:
            logger.exception("pr_sweep aged-skip surfacing failed to notify for %s", row.ref)
        announced.append(row.ref)
    # Stamped regardless of delivery: an undeliverable DM must not re-fire the same
    # announcement every tick — the doctor view keeps reporting it standing.
    SweepSkipStreak.objects.mark_surfaced([row.pk for row in due])
    return announced


def record_sweep_outcomes(
    signals: Iterable[ScanSignal],
    *,
    notify: SkipNotifier | None = None,
    threshold: int = SURFACE_AFTER_TICKS,
) -> list[str]:
    """Fold this tick's sweep signals into the streak ledger; announce the newly aged.

    Returns the refs announced this tick (empty on the common path).
    """
    sweep_signals = [signal for signal in signals if signal.kind.startswith(_SWEEP_PREFIX)]
    if not sweep_signals:
        return []
    try:
        for signal in sweep_signals:
            _observe(signal)
        return _announce_due(notify or _default_notify, threshold)
    except Exception:
        logger.exception("pr_sweep aged-skip surfacing failed")
        return []
