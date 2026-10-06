"""``t3 browser`` refuses loudly when it cannot run a step — never an empty, green-looking result."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

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
