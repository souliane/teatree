"""The live workflow-run read at ONE head, and its four-way classification (#4554)."""

import json
import os
import stat
from pathlib import Path

from teatree.core.modelkit.forge_readability import CHECKS_UNREADABLE
from teatree.core.review import head_workflow_runs
from teatree.core.review.head_workflow_runs import (
    classify_workflow_runs,
    live_checks_at,
    parse_workflow_run_pages,
    workflow_runs_argv,
)

_SLUG = "souliane/teatree"
_HEAD = "a" * 40


def _run(**overrides: object) -> dict[str, object]:
    run: dict[str, object] = {
        "name": "test (3.13)",
        "workflow_id": 1,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-08-01T00:00:00Z",
        "run_started_at": "2026-08-01T00:00:00Z",
    }
    run.update(overrides)
    return run


def _page(*runs: dict[str, object]) -> str:
    return json.dumps([{"total_count": len(runs), "workflow_runs": list(runs)}])


class TestArgvNamesTheWorkflowRunSurface:
    def test_argv_reads_actions_runs_at_the_head_sha(self) -> None:
        argv = workflow_runs_argv(slug=_SLUG, head_sha=_HEAD)

        assert argv[0] == "api"
        assert "--paginate" in argv
        assert "--slurp" in argv
        assert f"repos/{_SLUG}/actions/runs?head_sha={_HEAD}" in argv[-1]
        assert "per_page=" in argv[-1]

    def test_argv_never_reads_the_check_runs_tally(self) -> None:
        # The issue's AC2 made mechanical: the check-runs tally has served false `0 pending`
        # reads on this repo, so a regression back to it must turn this test red.
        assert "check-runs" not in " ".join(workflow_runs_argv(slug=_SLUG, head_sha=_HEAD))


class TestClassification:
    def test_all_success_is_green(self) -> None:
        assert classify_workflow_runs([_run()]).status == "green"

    def test_a_failure_conclusion_is_red(self) -> None:
        reading = classify_workflow_runs([_run(conclusion="failure")])

        assert reading.status == "failed"
        assert reading.is_failed
        assert "test (3.13)" in reading.detail

    def test_a_timed_out_conclusion_is_red(self) -> None:
        assert classify_workflow_runs([_run(conclusion="timed_out")]).status == "failed"

    def test_an_unfinished_run_is_pending_not_green(self) -> None:
        assert classify_workflow_runs([_run(status="in_progress", conclusion=None)]).status == "pending"

    def test_red_outranks_pending(self) -> None:
        runs = [_run(status="queued", conclusion=None, workflow_id=2), _run(conclusion="failure")]

        assert classify_workflow_runs(runs).status == "failed"

    def test_no_runs_at_all_is_unreadable_never_green(self) -> None:
        # GitHub is eventually consistent in the direction that LOOKS like success: the run
        # rows may not exist yet at a head whose CI is about to go red.
        reading = classify_workflow_runs([])

        assert reading.status == CHECKS_UNREADABLE
        assert reading.is_unreadable

    def test_an_unrecognised_conclusion_is_unreadable_never_green(self) -> None:
        assert classify_workflow_runs([_run(conclusion="cancelled")]).status == CHECKS_UNREADABLE

    def test_a_skipped_run_is_green(self) -> None:
        assert classify_workflow_runs([_run(conclusion="skipped")]).status == "green"

    def test_a_superseded_failure_does_not_outrank_its_own_rerun(self) -> None:
        # Same workflow + event re-run green: the older failed row is history, and reading it
        # as red would resurrect exactly the false refusals this ticket removes.
        runs = [
            _run(conclusion="failure", created_at="2026-08-01T00:00:00Z", run_started_at="2026-08-01T00:00:00Z"),
            _run(conclusion="success", created_at="2026-08-01T01:00:00Z", run_started_at="2026-08-01T01:00:00Z"),
        ]

        assert classify_workflow_runs(runs).status == "green"

    def test_the_newest_run_wins_whichever_order_the_api_returned_them(self) -> None:
        # GitHub orders newest-first, so the stale failure routinely arrives LAST.
        runs = [
            _run(conclusion="success", created_at="2026-08-01T01:00:00Z", run_started_at="2026-08-01T01:00:00Z"),
            _run(conclusion="failure", created_at="2026-08-01T00:00:00Z", run_started_at="2026-08-01T00:00:00Z"),
        ]

        assert classify_workflow_runs(runs).status == "green"

    def test_a_different_workflow_is_not_deduped_away(self) -> None:
        runs = [_run(), _run(workflow_id=2, name="lint", conclusion="failure")]

        assert classify_workflow_runs(runs).status == "failed"


class TestParsing:
    def test_flattens_pages(self) -> None:
        out = json.dumps(
            [
                {"total_count": 1, "workflow_runs": [_run()]},
                {"total_count": 1, "workflow_runs": [_run(workflow_id=2)]},
            ]
        )

        parsed = parse_workflow_run_pages(out)

        assert parsed is not None
        assert len(parsed) == 2

    def test_unparsable_body_is_no_evidence(self) -> None:
        assert parse_workflow_run_pages("<html>502</html>") is None

    def test_a_non_list_payload_is_no_evidence(self) -> None:
        # A REST error body ({"message": "Not Found"}) parses fine and carries no runs.
        assert parse_workflow_run_pages(json.dumps({"message": "Not Found"})) is None

    def test_empty_pages_are_no_evidence(self) -> None:
        assert parse_workflow_run_pages(json.dumps([{"total_count": 0, "workflow_runs": []}])) is None


def _stub_gh(tmp_path: Path, *, stdout: str, exit_code: int = 0) -> None:
    """A real `gh` on PATH, so the probe is exercised through its own subprocess call."""
    script = tmp_path / "gh"
    script.write_text(f"#!/bin/sh\ncat <<'JSON'\n{stdout}\nJSON\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"


class TestLiveChecksAt:
    def test_reads_a_green_head(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(os, "environ", dict(os.environ))
        _stub_gh(tmp_path, stdout=_page(_run()))

        assert live_checks_at(slug=_SLUG, head_sha=_HEAD).status == "green"

    def test_a_non_zero_exit_is_unreadable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(os, "environ", dict(os.environ))
        _stub_gh(tmp_path, stdout="", exit_code=1)

        reading = live_checks_at(slug=_SLUG, head_sha=_HEAD)

        assert reading.status == CHECKS_UNREADABLE
        assert "rc=1" in reading.detail

    def test_an_absent_gh_is_unreadable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(os, "environ", {**os.environ, "PATH": str(tmp_path)})

        assert live_checks_at(slug=_SLUG, head_sha=_HEAD).is_unreadable

    def test_an_unparsable_body_is_unreadable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(os, "environ", dict(os.environ))
        _stub_gh(tmp_path, stdout="<html>502 Bad Gateway</html>")

        reading = live_checks_at(slug=_SLUG, head_sha=_HEAD)

        assert reading.status == CHECKS_UNREADABLE
        assert "no readable run" in reading.detail

    def test_a_read_that_does_not_finish_is_unreadable(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(os, "environ", dict(os.environ))
        monkeypatch.setattr(head_workflow_runs, "READ_TIMEOUT_SECONDS", 0.2)
        script = tmp_path / "gh"
        script.write_text("#!/bin/sh\nsleep 5\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        os.environ["PATH"] = f"{tmp_path}{os.pathsep}{os.environ['PATH']}"

        reading = live_checks_at(slug=_SLUG, head_sha=_HEAD)

        assert reading.status == CHECKS_UNREADABLE
        assert "did not complete" in reading.detail
