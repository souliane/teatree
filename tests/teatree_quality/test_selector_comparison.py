"""Advisory divergence between our selector and the tach pytest plugin (#3672).

Advisory ONLY — nothing here changes what runs. The divergence set is the gate for a
later cutover, so the two directions are kept apart because they mean opposite things:
``ours_only`` is an escalation the plugin cannot infer (or our over-selection), while
``theirs_only`` is a test the plugin would KEEP and we do not select — the
under-selection direction, the only one that could ever produce a false green.
"""

from pathlib import Path

import pytest

from teatree.quality.affected_tests import Selection
from teatree.quality.selector_comparison import SelectorDivergence, advisory_divergence, compare_selection


class TestDivergenceDirections:
    _UNIVERSE = ("tests/a/test_a.py", "tests/b/test_b.py", "tests/c/test_c.py")

    def test_agreement_yields_no_divergence(self) -> None:
        divergence = compare_selection(
            selected=("tests/a/test_a.py",),
            would_skip=("tests/b/test_b.py", "tests/c/test_c.py"),
            universe=self._UNIVERSE,
            full=False,
        )
        assert divergence == SelectorDivergence(ours_only=(), theirs_only=(), full=False, comparable=True)

    def test_a_test_we_select_that_the_plugin_would_skip_is_ours_only(self) -> None:
        divergence = compare_selection(
            selected=("tests/a/test_a.py", "tests/b/test_b.py"),
            would_skip=("tests/b/test_b.py", "tests/c/test_c.py"),
            universe=self._UNIVERSE,
            full=False,
        )
        assert divergence.ours_only == ("tests/b/test_b.py",)
        assert divergence.theirs_only == ()

    def test_a_test_the_plugin_keeps_that_we_drop_is_theirs_only(self) -> None:
        divergence = compare_selection(
            selected=("tests/a/test_a.py",),
            would_skip=("tests/c/test_c.py",),
            universe=self._UNIVERSE,
            full=False,
        )
        assert divergence.theirs_only == ("tests/b/test_b.py",)
        assert divergence.under_selection_risk

    def test_agreement_carries_no_under_selection_risk(self) -> None:
        divergence = compare_selection(
            selected=("tests/a/test_a.py",),
            would_skip=("tests/b/test_b.py", "tests/c/test_c.py"),
            universe=self._UNIVERSE,
            full=False,
        )
        assert not divergence.under_selection_risk


class TestFullRunComparison:
    def test_a_full_run_selects_everything_so_nothing_can_be_under_selected(self) -> None:
        divergence = compare_selection(
            selected=(),
            would_skip=("tests/b/test_b.py",),
            universe=("tests/a/test_a.py", "tests/b/test_b.py"),
            full=True,
        )
        assert divergence.full
        assert divergence.theirs_only == ()
        assert divergence.ours_only == ("tests/b/test_b.py",)
        assert not divergence.under_selection_risk


class TestUnavailablePluginIsNotAgreement:
    """A missing would-skip set is UNKNOWN, never a silent "the two agree"."""

    def test_none_would_skip_is_not_comparable(self) -> None:
        divergence = compare_selection(
            selected=("tests/a/test_a.py",),
            would_skip=None,
            universe=("tests/a/test_a.py",),
            full=False,
        )
        assert not divergence.comparable
        assert divergence.ours_only == ()
        assert divergence.theirs_only == ()

    def test_an_incomparable_result_claims_no_under_selection_risk_either_way(self) -> None:
        divergence = compare_selection(selected=(), would_skip=None, universe=(), full=False)
        assert not divergence.under_selection_risk

    def test_report_names_the_unavailability_rather_than_reporting_agreement(self) -> None:
        report = compare_selection(selected=(), would_skip=None, universe=(), full=False).report()
        assert "unavailable" in report
        assert "agree" not in report


class TestReportIsActionable:
    def test_report_counts_both_directions(self) -> None:
        report = compare_selection(
            selected=("tests/a/test_a.py",),
            would_skip=("tests/c/test_c.py",),
            universe=("tests/a/test_a.py", "tests/b/test_b.py", "tests/c/test_c.py"),
            full=False,
        ).report()
        assert "1" in report
        assert "under-selection" in report


class TestAdvisoryDivergenceAgainstTheLiveTree:
    """The assembled advisory helper the observable lane calls — one call, never a run."""

    def test_a_scoped_selection_is_comparable_and_names_our_escalations(self) -> None:
        root = Path(__file__).resolve().parents[2]
        selection = Selection(
            full=False,
            reason="synthetic",
            test_files=("tests/teatree_quality/test_doc_impact.py",),
            floor_dirs=(),
        )
        divergence = advisory_divergence(root, selection)
        if not divergence.comparable:
            pytest.skip("tach impact probe unavailable in this environment")
        # The whole tree minus one selected file: the plugin keeps far more than we do,
        # so this asserts the helper wired a REAL universe rather than an empty one.
        assert divergence.theirs_only

    def test_a_full_selection_can_never_show_under_selection_risk(self) -> None:
        root = Path(__file__).resolve().parents[2]
        divergence = advisory_divergence(root, Selection(full=True, reason="synthetic FULL"))
        if not divergence.comparable:
            pytest.skip("tach impact probe unavailable in this environment")
        assert divergence.full
        assert not divergence.under_selection_risk
