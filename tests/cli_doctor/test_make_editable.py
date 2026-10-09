"""``DoctorService.make_editable`` — pyproject patching + dev-sources marker.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.doctor import DoctorService
from teatree.cli.doctor.app import doctor_app
from teatree.utils.run import run_allowed_to_fail


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
        '#!/bin/sh\nif [ "$1" = lock ]; then exit 0; fi\n'
        'echo "source = { editable = \\"../teatree\\" }" >> uv.lock\nexit 0\n',
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

        out = capsys.readouterr().out
        assert "now editable" in out
        assert f"t3 doctor cleanup-editable-sources {tmp_path}" in out
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

    def test_reports_fail_without_host_project_when_install_fails(self, tmp_path, capsys):
        failure = subprocess.CompletedProcess([], 1, "", "install failed")
        with (
            patch("teatree.cli.doctor.service._find_host_project_root", return_value=None),
            patch("subprocess.run", return_value=failure),
        ):
            DoctorService.make_editable("teatree", tmp_path)

        out = capsys.readouterr().out
        assert "FAIL  Could not install teatree as editable: install failed" in out
        assert "OK " not in out

    def test_reports_fail_when_uv_sync_fails(self, tmp_path, capsys):
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

        out = capsys.readouterr().out
        assert "FAIL  uv sync failed after patching sources: sync failed" in out
        assert "OK " not in out


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

    def test_restore_sources_unhides_lockfile(self, tmp_path, monkeypatch):
        _install_fake_uv(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
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

    def test_restore_sources_leaves_untracked_lockfile_and_finishes(self, tmp_path, monkeypatch):
        _install_fake_uv(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        repo = tmp_path / "host"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / "pyproject.toml").write_text('[project]\nname = "host"\n')
        _git(repo, "add", "pyproject.toml")
        _git(repo, "commit", "-q", "-m", "init")
        lockfile = repo / "uv.lock"
        lockfile.write_text("untracked = true\n")
        marker = repo / ".t3-dev-sources"
        marker.write_text("teatree=/repos/teatree\n")

        DoctorService.restore_sources(repo)

        assert lockfile.read_text() == "untracked = true\n"
        assert not marker.exists()

    def test_doctor_cleanup_restores_marked_source_files(self, tmp_path, monkeypatch):
        _install_fake_uv(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        repo = tmp_path / "host"
        repo.mkdir()
        _init_git_repo(repo)
        pyproject = repo / "pyproject.toml"
        lockfile = repo / "uv.lock"
        pyproject.write_text('[project]\nname = "host"\n')
        lockfile.write_text('[[package]]\nname = "teatree"\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        pyproject.write_text('[project]\nname = "host"\n[tool.uv.sources]\nteatree = { path = "/repos/teatree" }\n')
        lockfile.write_text('[[package]]\nname = "teatree"\nsource = { path = "/repos/teatree" }\n')
        _git(repo, "update-index", "--assume-unchanged", "pyproject.toml", "uv.lock")
        marker = repo / ".t3-dev-sources"
        marker.write_text("teatree=/repos/teatree\n")

        result = CliRunner().invoke(doctor_app, ["cleanup-editable-sources", str(repo)])

        assert result.exit_code == 0, result.output
        assert pyproject.read_text() == '[project]\nname = "host"\n'
        assert lockfile.read_text() == '[[package]]\nname = "teatree"\n'
        assert not marker.exists()

    def test_cleanup_keeps_unrelated_edits_in_both_files(self, tmp_path, monkeypatch):
        _install_fake_uv(tmp_path / "bin")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        repo = tmp_path / "host"
        repo.mkdir()
        _init_git_repo(repo)
        pyproject = repo / "pyproject.toml"
        lockfile = repo / "uv.lock"
        pyproject.write_text(
            '[project]\nname = "host"\n\n[tool.uv.sources]\nteatree = { git = "https://example.test/t3" }\n'
        )
        lockfile.write_text('[[package]]\nname = "teatree"\nsource = { registry = "https://pypi.org" }\n')
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        pyproject.write_text(
            '[project]\nname = "host"\nversion = "1.2.3"\n\n[tool.uv.sources]\n'
            'teatree = { path = "/repos/teatree", editable = true }\n'
        )
        lockfile.write_text(
            '[[package]]\nname = "teatree"\nsource = { editable = "/repos/teatree" }\n'
            'dependencies = [{ name = "extra" }]\n'
        )
        (repo / ".t3-dev-sources").write_text("teatree=/repos/teatree\n")

        result = CliRunner().invoke(doctor_app, ["cleanup-editable-sources", str(repo)])

        assert result.exit_code == 0, result.output
        assert 'version = "1.2.3"' in pyproject.read_text()
        assert 'teatree = { git = "https://example.test/t3" }' in pyproject.read_text()
        assert 'dependencies = [{ name = "extra" }]' in lockfile.read_text()
        assert 'source = { registry = "https://pypi.org" }' in lockfile.read_text()

    def test_restore_relocks_package_metadata_after_pyproject_restore(self, tmp_path):
        repo = tmp_path / "host"
        repo.mkdir()
        _init_git_repo(repo)
        pyproject = repo / "pyproject.toml"
        lockfile = repo / "uv.lock"
        pyproject.write_text(
            '[project]\nname = "host"\n\n[tool.uv.sources]\nteatree = { git = "https://example.test/t3" }\n'
        )
        lockfile.write_text(
            '[[package]]\nname = "host"\n[package.metadata]\n'
            'requires-dist = [{ name = "teatree", git = "https://example.test/t3" }]\n'
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "init")
        pyproject.write_text(
            '[project]\nname = "host"\n\n[tool.uv.sources]\nteatree = { path = "/repos/teatree", editable = true }\n'
        )
        lockfile.write_text(
            '[[package]]\nname = "host"\n[package.metadata]\n'
            'requires-dist = [{ name = "teatree", path = "/repos/teatree" }]\n'
        )
        (repo / ".t3-dev-sources").write_text("teatree=/repos/teatree\n")

        def relock(argv, **kwargs):
            if argv == ["uv", "lock"]:
                assert 'git = "https://example.test/t3"' in pyproject.read_text()
                lockfile.write_text(
                    '[[package]]\nname = "host"\n[package.metadata]\n'
                    'requires-dist = [{ name = "teatree", git = "https://example.test/t3" }]\n'
                )
                return subprocess.CompletedProcess(argv, 0, "", "")
            return run_allowed_to_fail(argv, **kwargs)

        with patch("teatree.cli.doctor.service.run_allowed_to_fail", side_effect=relock) as runner:
            DoctorService.restore_sources(repo)
        assert any(call.args[0] == ["uv", "lock"] for call in runner.call_args_list)
        assert 'git = "https://example.test/t3"' in lockfile.read_text()
