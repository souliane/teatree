import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.browser.keeper import LaunchFailure
from teatree.cli.doctor.checks_browser import INSTALL_ARGV, INSTALL_TIMEOUT_S, _check_browser_ready

_MISSING_BUILD = LaunchFailure("Executable doesn't exist", timed_out=False)


def test_a_missing_browser_build_fails_naming_the_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "no-browsers"))

    assert _check_browser_ready(repair=False) is False

    out = capsys.readouterr().out
    assert out.startswith("FAIL  the headless browser cannot launch")
    assert "t3 doctor check --repair" in out
    assert f"`{sys.executable} -m playwright install chromium-headless-shell`" in out


def test_repair_installs_the_browser_then_probes_again(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("teatree.browser.keeper.launch_probe", side_effect=[_MISSING_BUILD, None]) as probe,
        patch(
            "teatree.cli.doctor.checks_browser.run_allowed_to_fail",
            return_value=subprocess.CompletedProcess(INSTALL_ARGV, 0),
        ) as install,
    ):
        assert _check_browser_ready(repair=True) is True

    install.assert_called_once_with(INSTALL_ARGV, expected_codes=None, timeout=INSTALL_TIMEOUT_S)
    assert probe.call_count == 2
    assert capsys.readouterr().out.startswith("OK    headless browser launches")


def test_a_failed_install_keeps_the_failure(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("teatree.browser.keeper.launch_probe", return_value=_MISSING_BUILD) as probe,
        patch(
            "teatree.cli.doctor.checks_browser.run_allowed_to_fail",
            return_value=subprocess.CompletedProcess(INSTALL_ARGV, 1),
        ),
    ):
        assert _check_browser_ready(repair=True) is False

    assert probe.call_count == 1
    assert "FAIL" in capsys.readouterr().out


def test_playwright_missing_from_the_running_env_fails_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delitem(sys.modules, "teatree.browser.keeper", raising=False)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    with patch("teatree.cli.doctor.checks_browser.run_allowed_to_fail") as install:
        assert _check_browser_ready(repair=True) is False

    install.assert_not_called()
    out = capsys.readouterr().out
    assert out.startswith("FAIL  Playwright is not importable in the environment running `t3`")
    assert "t3 doctor check --repair" in out


def test_a_launch_that_only_timed_out_is_a_warning_not_a_broken_browser(capsys: pytest.CaptureFixture[str]) -> None:
    timed_out = LaunchFailure("BrowserType.launch: Timeout 15000ms exceeded.", timed_out=True)

    with (
        patch("teatree.browser.keeper.launch_probe", return_value=timed_out),
        patch("teatree.cli.doctor.checks_browser.run_allowed_to_fail") as install,
    ):
        assert _check_browser_ready(repair=True) is True

    install.assert_not_called()
    out = capsys.readouterr().out
    assert out.startswith("WARN  the headless browser did not launch within 15s")
    assert "FAIL" not in out
