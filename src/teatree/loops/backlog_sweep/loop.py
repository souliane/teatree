"""Backlog-sweep mini-loop — daily backlog-triage cadence anchor.

Global (non-overlay) loop like ``news`` / ``eval_local``, default-OFF: the
:func:`teatree.loop.global_scanner_factories._backlog_sweep_scanner` builder
returns ``None`` while ``backlog_sweep_disabled`` (default *true*) holds, so this
mini-loop contributes nothing until the operator opts in (#22). The sweep is
destructive-capable (it proposes closing stale issues) and keeps its
``ask_before_backlog_sweep_closes`` gate, so unlike the always-on news/eval
scanners its kill switch ships ON.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

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
)
