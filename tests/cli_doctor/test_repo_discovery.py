"""``DoctorService.find_teatree_repo`` / ``find_overlay_repo`` — repo discovery.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

from unittest.mock import patch

from teatree.cli.doctor import DoctorService

from ._shared import _stage_home


class TestFindTeatreeRepo:
    def test_finds_via_t3_repo_env(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        monkeypatch.setenv("T3_REPO", str(tmp_path))
        monkeypatch.chdir(tmp_path.parent)

        assert DoctorService.find_teatree_repo() == tmp_path

    def test_auto_detects_via_find_project_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("T3_REPO", raising=False)
        monkeypatch.chdir(tmp_path.parent)

        with patch("teatree.find_project_root", return_value=tmp_path):
            assert DoctorService.find_teatree_repo() == tmp_path

    def test_returns_none_when_env_missing_and_auto_detect_fails(self, tmp_path, monkeypatch):
        monkeypatch.delenv("T3_REPO", raising=False)
        monkeypatch.chdir(tmp_path)

        with patch("teatree.find_project_root", return_value=None):
            assert DoctorService.find_teatree_repo() is None

    def test_prefers_cwd_worktree_over_t3_repo_env(self, tmp_path, monkeypatch):
        main_clone = tmp_path / "main"
        main_clone.mkdir()
        (main_clone / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        worktree = tmp_path / "ac-123-ticket" / "teatree"
        worktree.mkdir(parents=True)
        (worktree / "pyproject.toml").write_text('[project]\nname = "teatree"\n')
        monkeypatch.setenv("T3_REPO", str(main_clone))
        monkeypatch.chdir(worktree)

        assert DoctorService.find_teatree_repo() == worktree


class TestFindOverlayRepo:
    def test_finds_overlay_in_workspace(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        # find_overlay_repo scans config.clone_root(), resolved from
        # T3_WORKSPACE_DIR — not the retired [teatree] workspace_dir TOML key.
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path))
        overlay_dir = tmp_path / "my-overlay"
        overlay_dir.mkdir()
        (overlay_dir / "pyproject.toml").write_text('[project]\nname = "my-overlay"\n')

        assert DoctorService.find_overlay_repo("my-overlay") == overlay_dir

    def test_returns_none_when_overlay_absent(self, tmp_path, monkeypatch):
        _stage_home(tmp_path, monkeypatch)
        # find_overlay_repo scans config.clone_root(), resolved from
        # T3_WORKSPACE_DIR — not the retired [teatree] workspace_dir TOML key.
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path))

        assert DoctorService.find_overlay_repo("nonexistent") is None
