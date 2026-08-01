"""A container-owned control DB SKIPS the pinned-regressions lane, never FAILS it.

The lane runs in the pre-push hook on the HOST. Since the control DB moved into a
container-only volume, every DB-writing check raises :class:`DbBoundaryError` there —
a TOPOLOGY fault (the containerized stack legitimately owns the DB read-write, so the
host connection is read-only), not an invariant violation. The old handler bucketed it
with genuine failures, so a clean diff reported `5 failed` and blocked the push while
naming regressions it had not caused. Five simultaneous false reds also train the
reader to skim past the one line that would flag a real one.

The fix is a SKIP with the reason named, not a bypass — so the tests below pin BOTH
halves: the boundary fault does not redden the lane, AND nothing else got softer.
"""

import pytest

from teatree.db.boundary import DbBoundaryError
from teatree.eval.regression_corpus import run_regression_corpus
from teatree.eval.regression_corpus_models import RegressionCheck


def _raising(exc: BaseException) -> RegressionCheck:
    def predicate() -> bool:
        raise exc

    return RegressionCheck(failure_class="probe", origin="probe", invariant="probe", predicate=predicate)


def _failing() -> RegressionCheck:
    """A check whose predicate cleanly reports the invariant violated."""
    return RegressionCheck(failure_class="probe", origin="probe", invariant="probe", predicate=lambda: False)


class TestContainerOwnedDbIsASkip:
    def test_boundary_error_does_not_fail_the_lane(self) -> None:
        report = run_regression_corpus((_raising(DbBoundaryError("owned by the container")),))
        assert report.ok is True
        assert report.failures == ()

    def test_it_is_recorded_as_skipped_not_as_a_pass(self) -> None:
        # The distinction that keeps this honest: the check is NOT asserted to have
        # passed, it is recorded as not-run. A silent green would be the bypass.
        (result,) = run_regression_corpus((_raising(DbBoundaryError("owned")),)).results
        assert result.skipped is True

    def test_the_reason_names_the_cause_and_the_remedy(self) -> None:
        # "Skip loudly": a reader must be able to tell this from a real pass, and be
        # told where the lane WOULD run.
        (result,) = run_regression_corpus((_raising(DbBoundaryError("owned by the stack")),)).results
        assert "container-owned" in result.detail
        assert "owned by the stack" in result.detail


class TestNothingElseGotSofter:
    def test_a_predicate_returning_false_still_fails(self) -> None:
        report = run_regression_corpus((_failing(),))
        assert report.ok is False
        assert len(report.failures) == 1

    def test_any_other_exception_still_fails_hard(self) -> None:
        # Only the topology fault is a skip. A crashing predicate remains a failure,
        # so this cannot become a general "errors are skips" escape hatch.
        report = run_regression_corpus((_raising(RuntimeError("boom")),))
        assert report.ok is False
        assert "RuntimeError" in report.failures[0].detail

    def test_a_database_error_subclass_is_not_swallowed(self) -> None:
        # DbBoundaryError is deliberately NOT a DatabaseError. Assert the narrow
        # catch really is narrow: a sibling RuntimeError does not qualify.
        class SiblingError(RuntimeError):
            pass

        assert run_regression_corpus((_raising(SiblingError("nope")),)).ok is False


class TestUnreachableControlDbSkipsBeforeThePreflight:
    """The pre-flight opens the DB itself, so the topology read must precede it."""

    def _db_check(self) -> RegressionCheck:
        return RegressionCheck(
            failure_class="probe",
            origin="probe",
            invariant="probe",
            predicate=lambda: True,
            needs_db=True,
        )

    def test_a_container_only_control_dir_skips_rather_than_erroring(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The host case: the canonical dir is a container-only mount that does not
        # exist here. Before this, the pre-flight died on a raw OperationalError
        # traceback before a single check had been classified.
        monkeypatch.setenv("T3_CONTROL_DB_DIR", "/nonexistent/container-only/control-db")
        report = run_regression_corpus((self._db_check(),))
        assert report.ok is True
        assert report.failures == ()
        (result,) = report.results
        assert result.skipped is True
        assert "container-only mount by design" in result.detail

    def test_the_preflight_is_not_even_appended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Proof the guard runs FIRST: no pre-flight row is produced at all, so
        # nothing tried to open the database.
        monkeypatch.setenv("T3_CONTROL_DB_DIR", "/nonexistent/container-only/control-db")
        report = run_regression_corpus((self._db_check(),))
        assert len(report.results) == 1

    def test_a_non_db_check_still_runs_normally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The skip is scoped to DB-backed checks. The git/FSM checks that need no
        # database must keep running on the host — that is most of this lane.
        monkeypatch.setenv("T3_CONTROL_DB_DIR", "/nonexistent/container-only/control-db")
        report = run_regression_corpus((_failing(),))
        assert report.ok is False
        assert len(report.failures) == 1
