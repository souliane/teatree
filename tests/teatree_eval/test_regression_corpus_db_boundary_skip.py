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

from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import connection

from teatree.db.boundary import ControlDbBoundary, DbBoundaryError
from teatree.eval import regression_corpus_schema
from teatree.eval.regression_corpus import run_regression_corpus
from teatree.eval.regression_corpus_models import RegressionCheck, RegressionReport


def _raising(exc: BaseException) -> RegressionCheck:
    def predicate() -> bool:
        raise exc

    return RegressionCheck(failure_class="probe", origin="probe", invariant="probe", predicate=predicate)


def _failing() -> RegressionCheck:
    """A check whose predicate cleanly reports the invariant violated."""
    return RegressionCheck(failure_class="probe", origin="probe", invariant="probe", predicate=lambda: False)


_CONTAINER_ONLY_DIR = "/nonexistent/container-only/control-db"


def _host_pointed_at_the_container_only_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host case: the control dir is a container-only mount AND the ORM is aimed at it.

    Both halves are the scenario. Setting the dir alone leaves the ORM pointed at some other,
    perfectly reachable database, and the lane's question is about the database its checks
    actually query — so a dir-only arrangement asserts a skip the real host would not take
    for the reason named here.
    """
    monkeypatch.setenv("T3_CONTROL_DB_DIR", _CONTAINER_ONLY_DIR)
    monkeypatch.setitem(connection.settings_dict, "NAME", f"{_CONTAINER_ONLY_DIR}/db.sqlite3")


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


class TestTheCorpusNeverOpensTheAmbientDb:
    """The DB-backed checks run on a fresh DB, so a DB this host may not write is never opened."""

    def _db_check(self) -> RegressionCheck:
        return RegressionCheck(
            failure_class="probe",
            origin="probe",
            invariant="probe",
            predicate=lambda: True,
            needs_db=True,
        )

    def _run_recording_the_migrated_db(self) -> tuple[RegressionReport, list[str]]:
        migrated: list[str] = []

        def migrate() -> list[str]:
            migrated.append(str(connection.settings_dict["NAME"]))
            return []

        with patch.object(regression_corpus_schema, "migrate_self_db", side_effect=migrate):
            report = run_regression_corpus((self._db_check(),))
        return report, migrated

    def test_a_host_aimed_at_the_container_only_db_runs_its_checks_on_a_fresh_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _host_pointed_at_the_container_only_db(monkeypatch)
        report, migrated = self._run_recording_the_migrated_db()
        (result,) = [r for r in report.results if r.check.failure_class == "probe"]
        assert result.skipped is False
        assert report.validated is True
        assert len(migrated) == 1
        assert not migrated[0].startswith(_CONTAINER_ONLY_DIR)
        assert connection.settings_dict["NAME"] == f"{_CONTAINER_ONLY_DIR}/db.sqlite3"

    def test_a_container_claimed_worktree_db_is_never_the_one_migrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "db.sqlite3"
        ControlDbBoundary(db, containerized=True).claim_for_container()
        monkeypatch.setattr("teatree.db.boundary.is_running_in_container", lambda: False)
        monkeypatch.setitem(connection.settings_dict, "NAME", str(db))

        report, migrated = self._run_recording_the_migrated_db()

        (result,) = [r for r in report.results if r.check.failure_class == "probe"]
        assert result.skipped is False
        assert migrated != [str(db)]
        assert not db.exists()

    def test_a_non_db_check_still_runs_normally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host_pointed_at_the_container_only_db(monkeypatch)
        report = run_regression_corpus((_failing(),))
        assert report.ok is False
        assert len(report.failures) == 1
