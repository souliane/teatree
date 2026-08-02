"""A green with skips is reported as NOT VALIDATED, and can be made to exit non-zero.

The skip half of the lane (souliane/teatree#4001) is deliberate: a topology fault is not
an invariant violation, so a container-owned control DB must not redden the pre-push
lane. What it left behind is souliane/teatree#4005 — a skipped check carries ``ok=True``,
so ``report.ok`` and the lane's exit code read identically whether the pins asserted
everything or nothing, and a caller consumes that green as truth.

The resolution keeps ``ok`` meaning "no failures" (the #4001 pins stay green) and adds the
missing second axis: ``validated`` says every check actually ran. The schema pre-flight is
reported as a skipped row rather than silently omitted, so "a failed migration passes
silently" becomes "the migration pre-flight is named as not-run".
"""

import json
from contextlib import AbstractContextManager
from unittest.mock import MagicMock, patch

import pytest
from django.db import connection
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.all import regression_lane
from teatree.eval.regression_corpus import render_json, render_text, run_regression_corpus
from teatree.eval.regression_corpus_models import CheckResult, RegressionCheck, RegressionReport

_CONTAINER_ONLY_DIR = "/nonexistent/container-only/control-db"


def _check(name: str = "probe", *, needs_db: bool = False) -> RegressionCheck:
    return RegressionCheck(
        failure_class=name,
        origin="https://example.com/x",
        invariant="probe",
        predicate=lambda: True,
        needs_db=needs_db,
    )


def _report(*results: CheckResult) -> RegressionReport:
    return RegressionReport(results=results)


def _ran(name: str = "probe") -> CheckResult:
    return CheckResult(check=_check(name), ok=True, skipped=False, detail="")


def _skipped(name: str = "probe") -> CheckResult:
    return CheckResult(check=_check(name), ok=True, skipped=True, detail="container-only mount")


def _corpus_returning(report: RegressionReport) -> AbstractContextManager[MagicMock]:
    return patch("teatree.cli.eval.lanes.run_regression_corpus", return_value=report)


def _host_pointed_at_the_container_only_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_CONTROL_DB_DIR", _CONTAINER_ONLY_DIR)
    monkeypatch.setitem(connection.settings_dict, "NAME", f"{_CONTAINER_ONLY_DIR}/db.sqlite3")


class TestValidatedIsTheSecondAxis:
    def test_a_fully_run_report_is_validated(self) -> None:
        assert _report(_ran()).validated is True

    def test_a_report_with_a_skip_is_green_but_not_validated(self) -> None:
        report = _report(_ran("a"), _skipped("b"))
        assert report.ok is True, "a topology skip must not redden the lane (#4001)"
        assert report.validated is False

    def test_skipped_exposes_the_checks_that_asserted_nothing(self) -> None:
        assert [r.check.failure_class for r in _report(_ran("a"), _skipped("b")).skipped] == ["b"]


class TestTheRenderersCarryTheDistinction:
    def test_json_names_validated_alongside_ok(self) -> None:
        payload = json.loads(render_json(_report(_ran("a"), _skipped("b"))))
        assert payload["ok"] is True
        assert payload["validated"] is False

    def test_json_validated_is_true_for_a_fully_run_lane(self) -> None:
        assert json.loads(render_json(_report(_ran())))["validated"] is True

    def test_text_summary_says_a_skipped_run_is_not_a_validated_green(self) -> None:
        assert "NOT a validated green" in render_text(_report(_ran("a"), _skipped("b")))

    def test_text_summary_stays_quiet_when_everything_ran(self) -> None:
        assert "NOT a validated green" not in render_text(_report(_ran()))


class TestThePreflightIsNamedNotOmitted:
    """The migrate pre-flight row must exist even when it could not run.

    Its absence is what made "a failed migration passes silently" literal: with no row,
    neither a renderer nor a caller could tell the pre-flight had been meant to run.
    """

    def test_an_unreachable_db_reports_the_preflight_as_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host_pointed_at_the_container_only_db(monkeypatch)
        report = run_regression_corpus((_check(needs_db=True),))
        preflight = [r for r in report.results if "runtime-schema migration" in r.check.failure_class]
        assert len(preflight) == 1, f"the pre-flight row must not vanish: {report.results}"
        assert preflight[0].skipped is True
        assert report.validated is False


class TestStrictDemandsAValidatedGreen:
    def test_default_exit_is_zero_so_the_host_pre_push_lane_still_passes(self) -> None:
        # Preserved from #4001: the host cannot reach the container-owned DB, and blocking
        # every push there is the false red the skip was introduced to end.
        with _corpus_returning(_report(_ran("a"), _skipped("b"))):
            result = CliRunner().invoke(app, ["eval", "pinned-regressions"])
        assert result.exit_code == 0, result.output

    def test_strict_exits_nonzero_when_a_check_could_not_run(self) -> None:
        with _corpus_returning(_report(_ran("a"), _skipped("b"))):
            result = CliRunner().invoke(app, ["eval", "pinned-regressions", "--strict"])
        assert result.exit_code == 1, result.output

    def test_strict_exits_zero_on_a_fully_run_green(self) -> None:
        with _corpus_returning(_report(_ran())):
            result = CliRunner().invoke(app, ["eval", "pinned-regressions", "--strict"])
        assert result.exit_code == 0, result.output


class TestTheSuiteLaneReadsTheSameAxis:
    def test_a_skipped_report_is_flagged_as_needing_setup(self) -> None:
        assert regression_lane(_report(_ran("a"), _skipped("b"))).needs_setup is True

    def test_a_fully_run_report_needs_no_setup(self) -> None:
        assert regression_lane(_report(_ran())).needs_setup is False
