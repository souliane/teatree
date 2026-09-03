"""How disk pressure scales the reclaim criterion, and when a stalled reclaim is an alarm (#4644).

Dormancy-by-mtime was the evictor's only size-relevant criterion, so a checkout
some other process rewrites more often than ``venv_idle_days`` was ineligible by
construction — not on this pass, but on every pass forever, however full the disk
got. Age is the wrong authority under pressure precisely because a ``.venv`` is a
``uv sync`` product: rebuilding one costs a re-sync, and nothing else, so on a
box that is out of disk the only thing worth asking is whether somebody is
working in that checkout.

So the criterion decays with measured free space, between two thresholds that
already ship. Below the critical floor it stops applying at all — expressed as
``None`` rather than a cutoff of "now", because a cutoff races a directory
written during the pass and that race is the churn hole this closes. Liveness is
untouched by any of it: :mod:`teatree.core.cleanup.venv_eviction` still refuses
the whole pass on an unreadable process table, and a checkout a live process is
inside is still never a candidate at any pressure.

Nothing here imports from teatree, so both the loop's freeing handler and core's
health collector can read it without a backwards edge.
"""

#: Consecutive zero-yield passes below the critical floor before the reclaim is
#: reported stalled. Not per-item, so not a setting: at the shipped 30-minute
#: freeing interval it is roughly 90 minutes of a full box freeing nothing.
ZERO_YIELD_ALARM_PASSES = 3


def effective_idle_days(
    *,
    free_gb: float | None,
    warn_gb: float | None,
    crit_gb: float | None,
    idle_days: float,
) -> float | None:
    """The dormancy the evictor should require now — ``None`` when age must not gate at all."""
    if free_gb is None or warn_gb is None or crit_gb is None or warn_gb <= crit_gb:
        return idle_days
    if free_gb < crit_gb:
        return None
    if free_gb >= warn_gb:
        return idle_days
    return idle_days * (free_gb - crit_gb) / (warn_gb - crit_gb)


def below_floor(*, free_gb: float | None, crit_gb: float | None) -> bool:
    """True only when a reading PROVES the disk is under the critical floor."""
    return free_gb is not None and crit_gb is not None and free_gb < crit_gb


def reclaim_is_stalled(*, streak: int, free_gb: float | None, crit_gb: float | None) -> bool:
    """A run of empty passes is only an alarm while the disk is still below the floor.

    Keying on the live free-space reading rather than the streak alone is what
    lets the alarm clear by any route — including one that stops the pass from
    running again, so the streak never resets.
    """
    return streak >= ZERO_YIELD_ALARM_PASSES and below_floor(free_gb=free_gb, crit_gb=crit_gb)


__all__ = ["ZERO_YIELD_ALARM_PASSES", "below_floor", "effective_idle_days", "reclaim_is_stalled"]
