"""``t3 browser`` refuses loudly when it cannot run a step — never an empty, green-looking result."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.browser.evidence import BrowserEvent
from teatree.browser.session import BrowserSession, StepFailedError, StepReport
from teatree.cli.browser import browser_app
from teatree.core.invocation_cwd import INVOCATION_CWD_ENV
from tests._git_repo import make_git_repo

runner = CliRunner()


@pytest.fixture(autouse=True)
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    monkeypatch.delenv(INVOCATION_CWD_ENV, raising=False)
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    return repo


@pytest.mark.parametrize("arguments", [["inspect"], ["act", "click", "text=Go"]])
def test_a_step_without_a_session_exits_1_naming_the_way_to_start_one(arguments: list[str]) -> None:
    result = runner.invoke(browser_app, arguments)

    assert result.exit_code == 1
    assert "no open browser session" in result.output
    assert "t3 browser open" in result.output


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["act", "fill", "#name"], "takes SELECTOR VALUE; got 1"),
        (["act", "click", "a", "b"], "takes SELECTOR; got 2"),
        (["act", "upload", "#file"], "takes SELECTOR FILE...; got 1"),
    ],
)
def test_act_refuses_the_wrong_number_of_arguments(arguments: list[str], expected: str) -> None:
    result = runner.invoke(browser_app, arguments)

    assert result.exit_code == 1
    assert expected in result.output


def test_open_without_a_browser_build_surfaces_the_launch_error_and_the_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))

    result = runner.invoke(browser_app, ["open", "about:blank"])

    assert result.exit_code == 1
    assert "did not launch" in result.output
    assert "t3 doctor check --repair" in result.output


def test_close_without_a_session_is_a_no_op() -> None:
    result = runner.invoke(browser_app, ["close"])

    assert result.exit_code == 0
    assert "No open browser session." in result.output


def test_close_reports_as_json_too() -> None:
    result = runner.invoke(browser_app, ["close", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"closed": False}


def test_a_failed_step_still_prints_what_the_page_did(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = [BrowserEvent("console", text="diag-before-timeout", level="error", seq=1)]

    def _timed_out(_session: BrowserSession, _url: str, *, timeout_s: float) -> StepReport:
        message = f"Timeout {timeout_s * 1000:.0f}ms exceeded"
        raise StepFailedError(message, recorded)

    monkeypatch.setattr(BrowserSession, "open", _timed_out)

    plain = runner.invoke(browser_app, ["open", "http://127.0.0.1:9/", "--timeout", "3"])
    as_json = runner.invoke(browser_app, ["open", "http://127.0.0.1:9/", "--timeout", "3", "--json"])

    assert plain.exit_code == as_json.exit_code == 1
    assert "CONSOLE error diag-before-timeout" in plain.stdout
    assert "ERROR Timeout 3000ms exceeded" in plain.stderr
    payload = json.loads(as_json.stdout)
    assert payload["error"] == "Timeout 3000ms exceeded"
    assert [event["text"] for event in payload["events"]] == ["diag-before-timeout"]


def test_open_waits_thirty_seconds_for_the_load_event_unless_told_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    def _open(_session: BrowserSession, _url: str, *, timeout_s: float) -> StepReport:
        waits.append(timeout_s)
        return StepReport(url="about:blank", title="", events=[])

    monkeypatch.setattr(BrowserSession, "open", _open)

    runner.invoke(browser_app, ["open", "about:blank"])
    runner.invoke(browser_app, ["open", "about:blank", "--timeout", "90"])

    assert waits == [30.0, 90.0]
