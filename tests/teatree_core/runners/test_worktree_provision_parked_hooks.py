"""Provisioning is the install's second consumer, and it used to say nothing about a parked gate.

The installer line reports it, ``t3 doctor check`` FAILs on it, and a worktree provisioned by
the loop reached neither surface — so a destroyed operator gate was invisible on exactly the
path that runs unattended.
"""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from teatree.core import hook_quarantine
from teatree.core.provision.provision_report import StepResult
from teatree.core.runners import worktree_provision
from teatree.utils.git_worktree_query import git_common_dir
from tests._git_repo import make_git_repo

_OPERATOR_GATE = b"#!/bin/sh\n# the operator's own pre-push gate\nexit 0\n"


def _park(repo: Path, name: str) -> Path:
    parked = hook_quarantine.quarantine_dir(git_common_dir(repo))
    parked.mkdir(parents=True, exist_ok=True)
    (parked / name).write_bytes(_OPERATOR_GATE)
    return parked / name


class TestProvisioningNamesAParkedHook:
    def test_a_parked_hook_is_warned_about(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        repo = make_git_repo(tmp_path / "repo")
        parked = _park(repo, "pre-push")

        with caplog.at_level(logging.WARNING, logger=worktree_provision.__name__):
            worktree_provision._warn_on_parked_hooks(str(repo))

        assert str(parked) in caplog.text
        assert "t3 doctor check" in caplog.text

    def test_a_clean_clone_is_silent(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """The control: a warning that always fires reports every provision run."""
        repo = make_git_repo(tmp_path / "repo")

        with caplog.at_level(logging.WARNING, logger=worktree_provision.__name__):
            worktree_provision._warn_on_parked_hooks(str(repo))

        assert caplog.text == ""

    def test_an_unreadable_quarantine_warns_rather_than_reading_as_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo(tmp_path / "repo")
        quarantine = hook_quarantine.quarantine_dir(git_common_dir(repo))
        quarantine.mkdir(parents=True)

        def _refuse(self: Path) -> None:
            raise PermissionError(13, "Permission denied", str(self))

        with (
            caplog.at_level(logging.WARNING, logger=worktree_provision.__name__),
            patch.object(Path, "iterdir", _refuse),
        ):
            worktree_provision._warn_on_parked_hooks(str(repo))

        assert str(quarantine) in caplog.text
        assert "could not be read" in caplog.text


class TestAFailedInstallStillReportsTheParkedHook:
    """The failing run is the one most likely to be leaving a hook parked, and it said nothing.

    The read sat AFTER the early return, so provisioning reported a parked gate only on the
    path where the quarantine is usually empty.
    """

    @staticmethod
    def _provision(wt_path: Path, *, install_succeeds: bool) -> None:
        (wt_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
        overlay = MagicMock()
        overlay.provisioning.envrc_lines.return_value = []
        result = StepResult(name="prek-install", success=install_succeeds, error="prek exploded")
        with patch.object(worktree_provision.prek_hook, "install", return_value=result):
            worktree_provision._setup_worktree_dir(str(wt_path), MagicMock(), overlay)

    def test_a_failed_install_still_reports_the_parked_hook(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = make_git_repo(tmp_path / "repo")
        parked = _park(repo, "pre-push")

        with caplog.at_level(logging.WARNING, logger=worktree_provision.__name__):
            self._provision(repo, install_succeeds=False)

        assert str(parked) in caplog.text

    def test_a_successful_install_with_a_clean_quarantine_warns_about_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The control: a read that always warns reports every provision run."""
        repo = make_git_repo(tmp_path / "repo")

        with caplog.at_level(logging.WARNING, logger=worktree_provision.__name__):
            self._provision(repo, install_succeeds=True)

        assert "NOT gating this clone" not in caplog.text
