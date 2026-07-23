"""Advisory diff between our selector and the tach pytest plugin (#3672).

ADVISORY ONLY. Nothing here changes what runs — it exists so the two selectors can be
diffed over real diffs before any cutover. Every divergence is either a bug in our
escalation policy or an escalation the plugin cannot infer, and that set is the gate
for letting the plugin actually deselect.

The two directions are deliberately NOT summed into one "difference" number, because
they carry opposite risk:

* ``ours_only`` — we select it, the plugin would skip it. Our doc-reader mapping, floor
    dirs, mirror rule and force-FULL escalations all land here. Costs time, never a false
    green.
* ``theirs_only`` — the plugin would KEEP it and we do not select it. This is the
    under-selection direction, the only one that can produce a false green, so it is
    surfaced separately as :attr:`SelectorDivergence.under_selection_risk`.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.affected_tests import Selection
from teatree.quality.tach_impact import would_skip_tests
from teatree.quality.test_path_mirror import collect_test_files


@dataclass(frozen=True)
class SelectorDivergence:
    """Where the two selectors disagree. ``comparable`` is False when tach had no answer."""

    ours_only: tuple[str, ...]
    theirs_only: tuple[str, ...]
    full: bool
    comparable: bool

    @property
    def under_selection_risk(self) -> bool:
        """True only when the plugin would keep a test we drop — the false-green direction."""
        return bool(self.theirs_only)

    def report(self) -> str:
        if not self.comparable:
            return "selector-comparison: tach impact unavailable — no advisory diff this run"
        scope = "FULL" if self.full else "SCOPED"
        return (
            f"selector-comparison ({scope}): {len(self.ours_only)} test(s) we select and tach would skip, "
            f"{len(self.theirs_only)} test(s) tach keeps and we drop (under-selection direction)"
        )


def compare_selection(
    *,
    selected: Iterable[str],
    would_skip: Iterable[str] | None,
    universe: Iterable[str],
    full: bool,
) -> SelectorDivergence:
    """Diff our *selected* set against the plugin's *would_skip* set over *universe*.

    ``would_skip is None`` means the plugin could not answer — reported as NOT comparable
    rather than as agreement, so an unavailable probe can never read as "the two match".
    A FULL run selects the whole *universe* by definition, so ``theirs_only`` is empty.
    """
    if would_skip is None:
        return SelectorDivergence(ours_only=(), theirs_only=(), full=full, comparable=False)

    all_tests = frozenset(universe)
    ours = all_tests if full else frozenset(selected)
    skipped = frozenset(would_skip)
    theirs = all_tests - skipped
    return SelectorDivergence(
        ours_only=tuple(sorted(ours & skipped)),
        theirs_only=tuple(sorted(theirs - ours)),
        full=full,
        comparable=True,
    )


def advisory_divergence(root: Path, selection: Selection) -> SelectorDivergence:
    """Diff *selection* against the plugin's verdict over every test file on disk.

    The one call the observable lane makes. It computes a verdict, never a run: the
    plugin's deselection hook is not involved, so this cannot change what pytest
    collects.
    """
    universe = tuple(path.relative_to(root).as_posix() for path in collect_test_files(root))
    return compare_selection(
        selected=selection.test_files,
        would_skip=would_skip_tests(root, candidates=universe),
        universe=universe,
        full=selection.full,
    )


__all__ = ["SelectorDivergence", "advisory_divergence", "compare_selection"]
