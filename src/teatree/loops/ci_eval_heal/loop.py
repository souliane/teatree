"""CI-eval self-healing mini-loop — advance open heal sessions (#3201 PR-3a).

Default-OFF (``default_enabled=False`` in the seed): the loop does nothing until an
operator enables its ``Loop`` row AND opens a session (``t3 eval ci-heal open``).
When enabled, it ticks on the live loop at a ~5m cadence; the scanner
(:mod:`teatree.loop.scanners.ci_eval_heal`) flags open sessions and the mechanical
handler advances each one FSM step — observe-only, never a fix (PR-3b).
"""

from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop

if TYPE_CHECKING:
    from teatree.loop.job_identity import _ScannerJob

_CADENCE_SECONDS = 300  # 5m live-tick poll; the Loop row's delay_seconds is the live source


def _build_jobs(**_: object) -> "list[_ScannerJob]":
    from teatree.loop.global_scanner_factories import _ci_eval_heal_scanner  # noqa: PLC0415 — tick-time import
    from teatree.loop.job_identity import _ScannerJob  # noqa: PLC0415 — deferred: loaded at fan-out, not import

    scanner = _ci_eval_heal_scanner()
    if scanner is None:
        return []
    return [_ScannerJob(scanner=scanner, overlay="")]


MINI_LOOP = MiniLoop(
    name="ci_eval_heal",
    default_cadence_seconds=_CADENCE_SECONDS,
    build_jobs=_build_jobs,
)
