"""``t3 eval ci-trigger`` / ``ci-status`` through the CLI with a faked gh client.

The two commands wrap ``gh`` via :class:`GhCiEvalClient`; these drive them through
the typer surface with the client factory faked (no real ``gh``), asserting the
dispatched workflow inputs, the head-SHA report, the resolved verdict, and — on a
failure — the reds parsed from a canned ``eval-heal-<sha>`` JSON with their
``triage_class``. A download failure surfaces a loud note, never a silent-empty
red set.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.backends.github.ci_eval_client import GhCiEvalClient
from teatree.cli import app
from teatree.cli.eval import ci_status as ci_status_mod
from teatree.cli.eval import ci_trigger as ci_trigger_mod
from teatree.cli.eval.ci_status import RedScenario, resolve_ci_status
from teatree.cli.eval.ci_trigger import trigger_ci_eval
from teatree.utils.run import CommandFailedError

_SHA = "0123456789abcdef0123456789abcdef01234567"


class _FakeClient:
    def __init__(self) -> None:
        self.repo = "souliane/teatree"
        self.token = ""
        self.triggered: dict[str, object] = {}
        self.runs: list[dict[str, object]] = []
        self.run_view: dict[str, object] = {}
        self.artifact_payload: dict[str, object] | None = None
        self.download_error: Exception | None = None

    def trigger_workflow(self, workflow: str, *, ref: str, inputs: dict[str, str]) -> None:
        self.triggered = {"workflow": workflow, "ref": ref, "inputs": inputs}

    def resolve_head_sha(self, ref: str) -> str:
        return _SHA

    def list_runs(self, workflow: str, *, branch: str, limit: int = 20) -> list[dict[str, object]]:
        return self.runs

    def view_run(self, run_id: int | str) -> dict[str, object]:
        return self.run_view

    def download_artifact(self, run_id: int | str, *, name: str, dest_dir: Path) -> None:
        if self.download_error is not None:
            raise self.download_error
        if self.artifact_payload is not None:
            (dest_dir / f"{name}.json").write_text(json.dumps(self.artifact_payload), encoding="utf-8")


class TestCiTriggerService:
    def test_dispatches_the_workflow_with_scenarios_and_credential(self) -> None:
        client = _FakeClient()
        result = trigger_ci_eval(client, ref="fix-branch", scenarios="a,b", credential="metered_api_key")
        assert client.triggered == {
            "workflow": "eval-ci-heal.yml",
            "ref": "fix-branch",
            "inputs": {"scenarios": "a,b", "credential": "metered_api_key", "pr_ref": "fix-branch"},
        }
        assert result.head_sha == _SHA
        assert result.triggered is True


class TestCiTriggerCommand:
    def test_prints_the_head_sha_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        monkeypatch.setattr(ci_trigger_mod, "build_ci_eval_client", lambda repo: client)
        result = CliRunner().invoke(app, ["eval", "ci-trigger", "--ref", "fix-branch"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ref"] == "fix-branch"
        assert payload["head_sha"] == _SHA
        assert client.triggered["inputs"]["credential"] == "subscription_oauth"  # type: ignore[index]

    def test_rejects_an_unknown_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The credential is validated BEFORE any client is built — a bad value
        # exits 2 without touching gh.
        built = False

        def _factory(repo: str) -> GhCiEvalClient:
            nonlocal built
            built = True
            return GhCiEvalClient(repo)

        monkeypatch.setattr(ci_trigger_mod, "build_ci_eval_client", _factory)
        result = CliRunner().invoke(app, ["eval", "ci-trigger", "--ref", "b", "--credential", "bogus"])
        assert result.exit_code == 2
        assert built is False


class TestCiStatusService:
    def test_resolves_the_newest_run_verdict_for_a_branch(self) -> None:
        client = _FakeClient()
        client.runs = [{"databaseId": 99, "headSha": _SHA}]
        client.run_view = {"status": "completed", "conclusion": "success", "headSha": _SHA, "url": "u"}
        report = resolve_ci_status(client, ref="fix-branch", run_id=None)
        assert report.found is True
        assert report.run_id == 99
        assert report.conclusion == "success"
        assert report.reds is None  # no reds fetched on a green run

    def test_not_found_when_no_run_exists(self) -> None:
        report = resolve_ci_status(_FakeClient(), ref="fix-branch", run_id=None)
        assert report.found is False
        assert report.reds is None

    def test_failure_parses_reds_with_their_triage_class(self) -> None:
        client = _FakeClient()
        client.run_view = {"status": "completed", "conclusion": "failure", "headSha": _SHA, "url": "u"}
        client.artifact_payload = {
            "scenarios": [
                {"name": "green_one", "lane": "clean_room", "triage_class": None},
                {"name": "red_one", "lane": "clean_room", "triage_class": "behavioral"},
                {"name": "flaky", "lane": "under_load", "triage_class": "infra_throttle"},
            ]
        }
        report = resolve_ci_status(client, ref="fix-branch", run_id="42")
        assert report.reds == (
            RedScenario(name="red_one", lane="clean_room", triage_class="behavioral"),
            RedScenario(name="flaky", lane="under_load", triage_class="infra_throttle"),
        )
        assert report.note == ""

    def test_failure_download_error_surfaces_a_loud_note(self) -> None:
        client = _FakeClient()
        client.run_view = {"status": "completed", "conclusion": "failure", "headSha": _SHA, "url": "u"}
        client.download_error = CommandFailedError(["gh", "run", "download"], 1, "", "artifact not found")
        report = resolve_ci_status(client, ref="fix-branch", run_id="42")
        assert report.reds is None
        assert "could not download" in report.note


class TestCiStatusCommand:
    def test_json_output_carries_the_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient()
        client.runs = [{"databaseId": 7, "headSha": _SHA}]
        client.run_view = {"status": "in_progress", "conclusion": None, "headSha": _SHA, "url": "u"}
        monkeypatch.setattr(ci_status_mod, "build_ci_eval_client", lambda repo: client)
        result = CliRunner().invoke(app, ["eval", "ci-status", "--ref", "fix-branch", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "in_progress"
        assert payload["run_id"] == 7

    def test_text_output_for_a_missing_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci_status_mod, "build_ci_eval_client", lambda repo: _FakeClient())
        result = CliRunner().invoke(app, ["eval", "ci-status", "--ref", "fix-branch"])
        assert result.exit_code == 0, result.output
        assert "no eval-ci-heal run found" in result.output
