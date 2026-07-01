"""Named single-responsibility phases of one loop tick.

``run_tick`` is a thin pipeline that composes these phases instead of
carrying the work inline:

*   :func:`scan_phase` — the read-then-signal stage: run the scan jobs in
    parallel and collect their signals + per-scanner errors.
*   :func:`sweep_phase` — split the mechanical maintenance scanners
    (``pr_sweep`` / ``self_update`` / ``pull_main_clone``) out of the scan
    fan-out so they are an explicit, named slice rather than three jobs
    buried in the parallel set. Both halves still run in the same tick and
    their signals are merged before dispatch, so behaviour is unchanged.
*   :func:`act_phase` — dispatch the collected signals into actions, run
    the inline mechanical handlers, and persist agent dispatches.
*   :func:`orchestrate_phase` — the speed-driven autonomous fan-out. A
    no-op at the default ``medium`` speed (today's behaviour); at ``slow``
    it admits at most one worker, and at ``full`` / ``boost`` it computes a
    claimed manifest of dispatchable work clamped to
    ``max_concurrent_auto_starts``. It only computes + claims + returns the
    manifest — spawning stays in the session/self-pump half.
*   :func:`render_phase` — the closing stage: project the dispatched
    actions into statusline zones, refresh the ``tick-meta.json`` /
    ``open-prs.json`` sidecars, plan the admit budget, fold in the
    live-loop / open-PR / t3-master anchors, and write the statusline.
"""

from teatree.loop.phases.act import act_phase
from teatree.loop.phases.orchestrate import ManifestEntry, OrchestrationManifest, orchestrate_phase
from teatree.loop.phases.render import render_phase
from teatree.loop.phases.scan import ScanOutcome, scan_phase
from teatree.loop.phases.sweep import SweepSplit, sweep_phase

__all__ = [
    "ManifestEntry",
    "OrchestrationManifest",
    "ScanOutcome",
    "SweepSplit",
    "act_phase",
    "orchestrate_phase",
    "render_phase",
    "scan_phase",
    "sweep_phase",
]
