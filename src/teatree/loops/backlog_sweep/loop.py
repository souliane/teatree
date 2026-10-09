"""Backlog-sweep mini-loop — daily backlog-grouping cadence anchor.

Global (non-overlay) loop like ``news`` / ``eval_local``. This row plus the active
preset are the single switch an operator flips — it seeds
``enabled = false`` and contributes nothing until they turn it on, the shape
``issue_implementer`` / ``triage_assessor`` / ``directive_loop`` already use. The sweep
groups aggressively and closes nothing for real, and keeps its
``ask_before_backlog_sweep_closes`` gate over every row retirement. A dream pass that
leaves gaps pending nudges it early through :func:`nudge_for_dream_gaps`.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415 (lazy import)
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 (lazy import)

    scanner = _backlog_sweep_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


def nudge_for_dream_gaps() -> bool:
    """Queue a sweep for the gaps a dream pass just left pending, when this loop admits work."""
    from teatree.loop.global_scanner_factories import _backlog_sweep_scanner  # noqa: PLC0415 (lazy import)
    from teatree.loops.enable_verdict import loop_admits  # noqa: PLC0415 (lazy import)

    if not loop_admits("backlog_sweep"):
        return False
    scanner = _backlog_sweep_scanner()
    return scanner is not None and bool(scanner.scan_dream_gaps())


MINI_LOOP = MiniLoop(
    name="backlog_sweep",
    default_cadence_seconds=86400,  # the sweep cadence itself — daily, with no inner gate
    build_jobs=_build_jobs,
    declared_reach=frozenset({LoopReach.EGRESS}),
    determinism=LoopDeterminism.AI,
)
