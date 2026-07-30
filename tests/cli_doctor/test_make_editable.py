"""``DoctorService.make_editable`` — pyproject patching + dev-sources marker.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor import DoctorService


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git`` inside *repo* (git is a trusted internal tool)."""
    return subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(root: Path) -> None:
    """Initialise a git repo at *root* with the user identity set."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")


def _install_fake_uv(bin_dir: Path) -> None:
    """Put a fake ``uv`` on PATH that rewrites ``uv.lock`` like ``uv sync`` does.

    Real ``uv sync`` re-resolves and overwrites the lockfile to record the
    editable local-path source.  The fake reproduces only that observable
    side effect (mutating ``uv.lock``) without the network/resolver.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        '#!/bin/sh\necho "source = { editable = \\"../teatree\\" }" >> uv.lock\nexit 0\n',
    )
    fake_uv.chmod(0o755)


class TestMakeEditable:
    """``make_editable`` shells out to ``uv``/``git``; those are the boundary mocks."""

    def test_success_patches_pyproject_and_writes_marker(self, tmp_path, capsys):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.uv.sources]\nteatree = { git = "https://example.com", branch = "main" }\n')
        (tmp_path / "manage.py").write_text("")

        success = subprocess.CompletedProcess([], 0)
        with (
            patch("teatree.cli.doctor.service._find_host_project_root", return_value=tmp_path),
            patch("subprocess.run", return_value=success),
        ):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "now editable" in capsys.readouterr().out
        assert (tmp_path / ".t3-dev-sources").is_file()
        rewritten = pyproject.read_text()
        assert "path =" in rewritten
        assert "editable = true" in rewritten

    def test_falls_back_to_ephemeral_install_without_host_project(self, capsys):
        success = subprocess.CompletedProcess([], 0)
        with (
            patch("teatree.cli.doctor.service._find_host_project_root", return_value=None),
            patch("subprocess.run", return_value=success),
        ):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "ephemeral" in capsys.readouterr().out

    def test_reports_warn_when_pyproject_has_no_source_entry(self, tmp_path, capsys):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "myproject"\n')
        (tmp_path / "manage.py").write_text("")

        with patch("teatree.cli.doctor.service._find_host_project_root", return_value=tmp_path):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "uv tool install" in capsys.readouterr().out

    def test_reports_fail_without_host_project_when_install_fails(self, tmp_path):
        failure = subprocess.CompletedProcess([], 1, "", "install failed")
        with (
            patch("teatree.cli.doctor.service._find_host_project_root", return_value=None),
            patch("subprocess.run", return_value=failure),
        ):
            DoctorService.make_editable("teatree", tmp_path)

    def test_reports_fail_when_uv_sync_fails(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test"\n\n[tool.uv.sources]\nteatree = { git = "https://x" }\n',
        )
        failure = subprocess.CompletedProcess([], 1, "", "sync failed")
        with (
            patch("teatree.cli.doctor.service._find_host_project_root", return_value=tmp_path),
            patch("subprocess.run", return_value=failure),
        ):
            DoctorService.make_editable("teatree", Path("/repos/teatree"))


class TestMakeEditableDoesNotLeakLockfile:
    """contribute=true editable install must not mutate the committed lockfile.

    The editable install patches ``pyproject.toml`` and runs ``uv sync``, which
    rewrites ``uv.lock`` to record the local-path source.  That dev-only mutation
    must stay out of the committed lockfile — git must report ``uv.lock`` clean
    after the install, exactly as it does for ``pyproject.toml``.
    """

    def test_uv_lock_is_clean_in_git_after_editable_install(self, tmp_path, monkeypatch):
        repo = tmp_path / "host"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / "manage.py").write_text("")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "host"\n\n[tool.uv.sources]\nteatree = { git = "https://x" }\n',
        )
        (repo / "uv.lock").write_text('name = "teatree"\nsource = { registry = "https://pypi.org" }\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")

        bin_dir = tmp_path / "bin"
        _install_fake_uv(bin_dir)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

        with patch("teatree.cli.doctor.service._find_host_project_root", return_value=repo):
            DoctorService.make_editable("teatree", tmp_path / "teatree")

        dirty = _git(repo, "status", "--porcelain").stdout
        assert "uv.lock" not in dirty, f"editable install leaked uv.lock into the commit path: {dirty!r}"

    def test_restore_sources_unhides_lockfile(self, tmp_path):
        repo = tmp_path / "host"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / "pyproject.toml").write_text('[project]\nname = "host"\n')
        (repo / "uv.lock").write_text('name = "teatree"\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        _git(repo, "update-index", "--assume-unchanged", "uv.lock")
        (repo / ".t3-dev-sources").write_text("teatree=/repos/teatree\n")

        DoctorService.restore_sources(repo)

        # No skip-worktree / assume-unchanged bit should remain on uv.lock.
        lsfiles = _git(repo, "ls-files", "-v", "uv.lock").stdout
        assert lsfiles.startswith("H "), f"uv.lock still hidden after restore: {lsfiles!r}"
