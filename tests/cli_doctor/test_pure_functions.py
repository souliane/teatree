"""Pure-function doctor modules — root/marker/restore/main-clone resolution.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch

import teatree.cli.doctor as teatree_cli_doctor
from teatree.cli.doctor import DoctorService


def _seed_cold_registry(db: Path, overlays: dict[str, dict]) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlays', ?)",
            (json.dumps(overlays),),
        )
        conn.commit()
    finally:
        conn.close()


class TestFindHostProjectRoot:
    def test_finds_project_in_current_dir(self, tmp_path, monkeypatch):
        (tmp_path / "manage.py").write_text("")
        (tmp_path / "pyproject.toml").write_text("")
        monkeypatch.chdir(tmp_path)

        assert teatree_cli_doctor._find_host_project_root() == tmp_path

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        assert teatree_cli_doctor._find_host_project_root() is None


class TestWriteDevSourcesMarker:
    def test_creates_new_marker_file(self, tmp_path):
        marker = tmp_path / ".t3-dev-sources"
        teatree_cli_doctor._write_dev_sources_marker(marker, "teatree", Path("/repos/teatree"))
        assert "teatree=/repos/teatree" in marker.read_text()

    def test_updates_existing_entry_in_place(self, tmp_path):
        marker = tmp_path / ".t3-dev-sources"
        marker.write_text("teatree=/old/path\nother=/other/path\n")
        teatree_cli_doctor._write_dev_sources_marker(marker, "teatree", Path("/new/path"))
        content = marker.read_text()
        assert "teatree=/new/path" in content
        assert "other=/other/path" in content
        assert "/old/path" not in content


class TestRestoreSources:
    def test_reverts_from_marker_via_git(self, tmp_path):
        marker = tmp_path / ".t3-dev-sources"
        marker.write_text("teatree=/repos/teatree\n")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as mock_run:
            DoctorService.restore_sources(tmp_path)

        assert not marker.exists()
        # (update-index + checkout) for pyproject.toml and uv.lock.
        assert mock_run.call_count == 4

    def test_noop_when_no_marker(self, tmp_path):
        DoctorService.restore_sources(tmp_path)  # must not raise


class TestResolveMainClone:
    """``_resolve_main_clone`` follows ``.git`` worktree pointer files.

    On a fresh CI clone, ``.git`` is a directory and the worktree-branch is
    skipped. The worktree case must be exercised explicitly here so it's
    covered regardless of where the test runs.
    """

    def test_walks_gitdir_pointer_to_main_clone(self, tmp_path, monkeypatch):
        monkeypatch.delenv("T3_REPO", raising=False)
        main_clone = tmp_path / "main"
        main_clone.mkdir()
        (main_clone / ".git").mkdir()
        (main_clone / "pyproject.toml").write_text("")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # .git file points at <main>/.git/worktrees/<name>
        worktree_gitdir = main_clone / ".git" / "worktrees" / "wt"
        (worktree / ".git").write_text(f"gitdir: {worktree_gitdir}\n", encoding="utf-8")

        with patch.object(DoctorService, "find_teatree_repo", return_value=worktree):
            assert teatree_cli_doctor._resolve_main_clone() == main_clone

    def test_returns_worktree_when_pointer_unreadable(self, tmp_path, monkeypatch):
        monkeypatch.delenv("T3_REPO", raising=False)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        with patch.object(DoctorService, "find_teatree_repo", return_value=worktree):
            assert teatree_cli_doctor._resolve_main_clone() == worktree


class TestCheckLegacyOverlayAlias:
    """``t3 doctor`` warns (never rewrites) on a stale legacy alias entry.

    souliane/teatree#1108: a bare ``teatree`` entry written by older
    ``slack-bot`` runs maps to the canonical ``t3-teatree`` overlay. The
    doctor surfaces it as a WARN with the rename; it must not mutate the
    user's DB overlays registry. The registry is read via the pre-Django
    ``cold_reader``, so the seed goes in a cold-readable DB.
    """

    def _run(self, tmp_path, monkeypatch, overlays: dict[str, dict]) -> str:
        import io  # noqa: PLC0415
        from contextlib import redirect_stdout  # noqa: PLC0415
        from unittest.mock import MagicMock  # noqa: PLC0415

        db = tmp_path / "config.sqlite3"
        _seed_cold_registry(db, overlays)
        monkeypatch.setenv("T3_CONFIG_DB", str(db))

        real_ep = MagicMock()
        real_ep.name = "t3-teatree"
        real_ep.value = "teatree.contrib.t3_teatree.overlay:TeatreeOverlay"

        out = io.StringIO()
        with (
            patch("importlib.metadata.entry_points", return_value=[real_ep]),
            redirect_stdout(out),
        ):
            teatree_cli_doctor._check_legacy_overlay_alias()
        return out.getvalue()

    def test_warns_on_stale_bare_alias_entry(self, tmp_path, monkeypatch):
        message = self._run(tmp_path, monkeypatch, {"teatree": {"mode": "auto"}})
        assert "WARN" in message
        assert "'teatree'" in message
        assert "t3-teatree" in message

    def test_silent_when_canonical_entry_used(self, tmp_path, monkeypatch):
        message = self._run(tmp_path, monkeypatch, {"t3-teatree": {"mode": "auto"}})
        assert message == ""

    def test_silent_when_alias_entry_has_path(self, tmp_path, monkeypatch):
        # A real path-backed overlay that merely happens to share a short
        # name is a deliberate distinct overlay, not a stale alias.
        message = self._run(tmp_path, monkeypatch, {"teatree": {"path": "/tmp/x"}})
        assert message == ""
