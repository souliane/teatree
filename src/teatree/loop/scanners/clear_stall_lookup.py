"""Which standing merge authorisations are genuinely stalled — the loop's shared read (#4250).

The loop side of :mod:`teatree.core.merge.clear_liveness`: it pairs the canonical backlog
population with the classifier and hands back only the CLEARs the forge reports OPEN.
Sits beside :mod:`~teatree.loop.scanners.pr_sweep_clear_lookup` because both answer a
CLEAR question for the loop, and because ``teatree.loop.self_improve`` already reads this
package — routing the detector through it keeps the ``teatree.core`` fan-in frozen.
"""

from datetime import datetime

from teatree.core.factory.merge_backlog import unconsumed_actionable_clear_rows
from teatree.core.merge.clear_liveness import PROBE_CAP, ClearLiveness, PrStateReader, probe, unverified_reader
from teatree.core.models.merge_clear import MergeClear

__all__ = ["PROBE_CAP", "PrStateReader", "live_pr_state_reader", "stalled_clears", "unverified_reader"]


def live_pr_state_reader() -> PrStateReader:
    """The production forge reader, resolved at tick time.

    Exposed here so a consumer in ``teatree.loop.self_improve`` arms the real reader
    without taking its own edge onto ``teatree.backends``.
    """
    from teatree.backends.loader import pr_open_state  # noqa: PLC0415 — deferred: loaded at tick time

    return pr_open_state


def stalled_clears(
    *,
    issued_before: datetime | None = None,
    read_state: PrStateReader = unverified_reader,
    cap: int = PROBE_CAP,
) -> list[MergeClear]:
    """The standing merge authorisations whose PR the forge still reports OPEN, oldest first.

    A row that merged or closed outside the keystone, and a row whose state cannot be
    read, are both excluded — absence of a local ``MergeAudit`` is not evidence that no
    merge happened. *issued_before* applies the caller's own staleness threshold before
    any forge read, so the probe cap is spent on rows that already qualify.
    """
    population = unconsumed_actionable_clear_rows("")
    if issued_before is not None:
        population = [clear for clear in population if clear.issued_at <= issued_before]
    return probe(population, read=read_state, cap=cap).of(ClearLiveness.STALLED)
