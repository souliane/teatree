"""The deterministic regression corpus must catch each failure class.

For every :class:`RegressionCheck` this test asserts two things:

* the live (fixed) code path satisfies the invariant — the check is GREEN, and
* the check is NOT vacuous — a broken stand-in for the same code path turns the corpus RED.

A check that stays GREEN against the pre-fix behavior would guard nothing.

The corpus runs under :class:`~django.test.TestCase` so the DB-backed checks
(merge precondition, loop-lease, migration graph) execute against the test DB.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.db.migrations.graph import MigrationGraph
from django.test import TestCase

from teatree.core import branch_currency, merge_execution
from teatree.core.models import LoopLease
from teatree.eval import regression_corpus
from teatree.eval.regression_corpus import RegressionCheck, _count_core_leaves, run_regression_corpus


def _linear_core_graph() -> MigrationGraph:
    graph = MigrationGraph()
    graph.add_node(("core", "0001"), None)
    graph.add_node(("core", "0002"), None)
    graph.add_dependency(None, ("core", "0002"), ("core", "0001"))
    return graph


def _forked_core_graph() -> MigrationGraph:
    graph = MigrationGraph()
    graph.add_node(("core", "0001"), None)
    graph.add_node(("core", "0002_a"), None)
    graph.add_node(("core", "0002_b"), None)
    graph.add_dependency(None, ("core", "0002_a"), ("core", "0001"))
    graph.add_dependency(None, ("core", "0002_b"), ("core", "0001"))
    return graph


class TestRegressionCorpusGreen(TestCase):
    def test_every_check_passes_on_the_fixed_code(self) -> None:
        report = run_regression_corpus()
        failures = [(r.check.failure_class, r.detail) for r in report.failures]
        assert report.ok, f"regression corpus went RED on the fixed code: {failures}"
        assert all(not r.skipped for r in report.results), "no check should skip when Django is configured"

    def test_corpus_covers_the_named_failure_classes(self) -> None:
        classes = {c.failure_class for c in regression_corpus._CHECKS}
        for needle in (
            "branch-currency",
            "substrate-merge",
            "maker≠checker",
            "loop-owner hijack",
            "migration-fork",
        ):
            assert any(needle in c for c in classes), f"no regression check covers {needle!r}"

    def test_every_origin_is_a_clickable_url(self) -> None:
        for check in regression_corpus._CHECKS:
            assert check.origin.startswith("https://"), f"{check.failure_class} origin must be a clickable URL"

    def test_corpus_stays_green_under_a_hijacked_git_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            outer = Path(raw)
            env = {
                "GIT_DIR": str(outer / "outer.git"),
                "GIT_INDEX_FILE": str(outer / "index"),
                "GIT_WORK_TREE": str(outer),
            }
            with patch.dict("os.environ", env):
                report = run_regression_corpus()
        failures = [(r.check.failure_class, r.detail) for r in report.failures]
        assert report.ok, f"corpus went RED under a hijacked GIT_* env (git-hook context): {failures}"


class TestRegressionCorpusAntiVacuous(TestCase):
    def test_branch_currency_check_fails_when_it_blocks_a_clean_branch(self) -> None:
        with patch.object(branch_currency, "sha_conflicts_with_target", return_value=object()):
            report = run_regression_corpus()
        assert not report.ok
        assert any("branch-currency" in r.check.failure_class for r in report.failures)

    def test_merge_floor_check_fails_when_authorization_guard_is_a_noop(self) -> None:
        with patch.object(merge_execution, "_assert_clear_authorized", return_value=None):
            report = run_regression_corpus()
        assert not report.ok
        floor = [r.check.failure_class for r in report.failures]
        assert any("substrate-merge" in c for c in floor)
        assert any("maker≠checker" in c for c in floor)

    def test_loop_lease_check_fails_when_an_alive_owner_is_hijacked(self) -> None:
        original = LoopLease.objects.claim_ownership

        def _always_win(name, **kwargs):
            original(name, **kwargs)
            return True, kwargs.get("session_id", "")

        with patch.object(type(LoopLease.objects), "claim_ownership", _always_win):
            report = run_regression_corpus()
        assert not report.ok
        assert any("loop-owner hijack" in r.check.failure_class for r in report.failures)

    def test_leaf_count_predicate_flags_a_synthetic_forked_graph(self) -> None:
        assert _count_core_leaves(_linear_core_graph()) == 1
        assert _count_core_leaves(_forked_core_graph()) > 1

    def test_migration_fork_check_fails_when_the_live_graph_forks(self) -> None:
        with patch.object(regression_corpus, "_count_core_leaves", return_value=2):
            report = run_regression_corpus()
        assert not report.ok
        assert any("migration-fork" in r.check.failure_class for r in report.failures)

    def test_skips_db_checks_when_django_not_configured(self) -> None:
        with patch.object(regression_corpus, "_django_ready", return_value=False):
            report = run_regression_corpus()
        db_results = [r for r in report.results if r.check.needs_db]
        assert db_results
        assert all(r.skipped and r.ok for r in db_results)


class TestRegressionCheckRaisingPredicate(TestCase):
    def test_a_raising_predicate_is_a_failure_not_a_crash(self) -> None:
        def _boom() -> bool:
            msg = "boom"
            raise RuntimeError(msg)

        check = RegressionCheck(
            failure_class="synthetic",
            origin="https://example.com/x",
            invariant="never",
            predicate=_boom,
        )
        report = run_regression_corpus(checks=(check,))
        assert not report.ok
        assert report.results[0].detail.startswith("RuntimeError")
