import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.doctor.checks_browser import INSTALL_ARGV, INSTALL_TIMEOUT_S, _check_browser_ready


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
        patch("teatree.browser.keeper.launch_probe", side_effect=["Executable doesn't exist", None]) as probe,
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
        patch("teatree.browser.keeper.launch_probe", return_value="Executable doesn't exist") as probe,
        patch(
            "teatree.cli.doctor.checks_browser.run_allowed_to_fail",
            return_value=subprocess.CompletedProcess(INSTALL_ARGV, 1),
        ),
    ):
        assert _check_browser_ready(repair=True) is False

    assert probe.call_count == 1
    assert "FAIL" in capsys.readouterr().out
