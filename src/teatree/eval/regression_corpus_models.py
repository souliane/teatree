"""Dataclasses for the deterministic regression corpus.

The check definition and the result/report shapes the runner produces and the
renderer reads, kept in one place so :mod:`teatree.eval.regression_corpus`
(running) and :mod:`teatree.eval.regression_corpus_report` (rendering) share
them without a circular import.
"""

import dataclasses
from collections.abc import Callable


@dataclasses.dataclass(frozen=True)
class RegressionCheck:
    """One real-code-path regression check for a named failure class."""

    failure_class: str
    origin: str
    invariant: str
    predicate: Callable[[], bool]
    needs_db: bool = False


@dataclasses.dataclass(frozen=True)
class CheckResult:
    check: RegressionCheck
    ok: bool
    skipped: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class RegressionReport:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.ok and not r.skipped)

    @property
    def skipped(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.skipped)

    @property
    def validated(self) -> bool:
        """Every check actually ran — the axis ``ok`` deliberately cannot carry (#4005).

        A skipped check reports ``ok=True`` so a topology fault never reddens the lane
        (#4001), which leaves an all-skipped run indistinguishable from a real green to
        anything reading ``ok`` alone. A caller that needs the pins to have asserted
        something reads this instead.
        """
        return not self.skipped
