"""Pane-reaper mini-loop — demote idle maker panes past the idle threshold (#1838 PR#7b).

A sibling of :mod:`teatree.loops.idle_stack_reaper`. PR#7a deferred this
registration to avoid the inertness gate; now that the maker consumer wires
teams into the live path, the pane reaper registers as a mini-loop. It is ONLY
active when ``teams_enabled``: :func:`_pane_reaper_scanner` returns ``None`` (so
``build_jobs`` returns ``[]``) while the feature is off, and the scanner itself
no-ops on a disabled flag — DEFAULT-OFF, byte-identical to today.
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_REGISTRY_CADENCE_FLOOR = 300  # 5 minutes — an idle pane is demoted on a slow cadence.


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _pane_reaper_scanner  # noqa: PLC0415
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415

    scanner = _pane_reaper_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="pane_reaper",
    default_cadence_seconds=_REGISTRY_CADENCE_FLOOR,
    build_jobs=_build_jobs,
)
