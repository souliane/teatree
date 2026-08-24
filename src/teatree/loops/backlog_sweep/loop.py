"""Backlog-sweep mini-loop — daily backlog-grouping cadence anchor.

Global (non-overlay) loop like ``news`` / ``eval_local``. ``backlog_sweep_disabled``
ships *open* (#4344), so this row is the single switch an operator flips — it seeds
``enabled = false`` and contributes nothing until they turn it on, the shape
``issue_implementer`` / ``triage_assessor`` / ``directive_loop`` already use. The sweep
groups aggressively and closes nothing for real, and keeps its
``ask_before_backlog_sweep_closes`` gate over every row retirement.
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


MINI_LOOP = MiniLoop(
    name="backlog_sweep",
    default_cadence_seconds=86400,  # 1d tick rate — weekly sweep cadence enforced internally
    build_jobs=_build_jobs,
    declared_reach=frozenset({LoopReach.EGRESS}),
    determinism=LoopDeterminism.AI,
)
