"""Tests for wt_setup.py -- edge cases and contract verification.

Happy path covered by test_integration_pipeline.py.
"""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from wt_setup import wt_setup


class TestWtSetup:
    @pytest.fixture
    def _setup_env(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir / "my-project")

    @pytest.fixture
    def mock_externals(self) -> Iterator[dict[str, MagicMock]]:
        """Mock all extension points and subprocess calls."""
        with (
            patch("lib.init.init"),
            patch("wt_setup.ext") as mock_ext,
            patch("wt_setup.db_exists", return_value=True) as mock_db,
            patch("wt_setup.subprocess.run") as mock_run,
        ):
            mock_ext.side_effect = lambda point, *_a, **_kw: _a[0] if point == "wt_detect_variant" and _a else None
            mock_run.return_value = MagicMock(returncode=0)
            yield {
                "ext": mock_ext,
                "db_exists": mock_db,
                "subprocess": mock_run,
            }

    @pytest.mark.usefixtures("_setup_env", "mock_externals")
    def test_replaces_stale_real_repo_env_file_with_symlink(
        self,
        ticket_dir: Path,
    ) -> None:
        """If a real .env.worktree exists in repo dir, replace with symlink."""
        real_file = ticket_dir / "my-project" / ".env.worktree"
        real_file.write_text("OLD_DATA=stale")

        wt_setup(variant="acme")

        assert real_file.is_symlink()
        assert real_file.resolve() == (ticket_dir / ".env.worktree").resolve()

    @pytest.mark.usefixtures("_setup_env")
    def test_db_name_without_variant(
        self,
        ticket_dir: Path,
        mock_externals: dict[str, MagicMock],
    ) -> None:
        mock_externals["ext"].side_effect = lambda point, *_a, **_kw: "" if point == "wt_detect_variant" else None
        wt_setup(variant="")

        content = (ticket_dir / ".env.worktree").read_text()
        assert "WT_DB_NAME=wt_1234\n" in content

    @pytest.mark.usefixtures("_setup_env")
    def test_calls_extension_points_in_order(
        self,
        mock_externals: dict[str, MagicMock],
    ) -> None:
        wt_setup(variant="acme")

        ext_calls = [c.args[0] for c in mock_externals["ext"].call_args_list]
        assert ext_calls == [
            "wt_detect_variant",
            "wt_symlinks",
            "wt_env_extra",
            "wt_services",
            "wt_post_db",
        ]

    @pytest.mark.usefixtures("_setup_env")
    def test_allows_direnv_on_worktree(
        self,
        mock_externals: dict[str, MagicMock],
    ) -> None:
        wt_setup(variant="acme")

        direnv_calls = [c for c in mock_externals["subprocess"].call_args_list if "direnv" in c.args[0]]
        assert len(direnv_calls) == 1
        assert "allow" in direnv_calls[0].args[0]

    @pytest.mark.usefixtures("_setup_env")
    def test_skips_db_restore_when_exists(
        self,
        mock_externals: dict[str, MagicMock],
    ) -> None:
        mock_externals["db_exists"].return_value = True
        wt_setup(variant="acme")

        ext_calls = [c.args[0] for c in mock_externals["ext"].call_args_list]
        assert "wt_db_import" not in ext_calls

    def test_returns_error_when_resolve_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path / "workspace"))
        monkeypatch.chdir(tmp_path)
        with (
            patch("lib.init.init"),
            patch("wt_setup.ext"),
        ):
            result = wt_setup()
        assert result == 1

    @pytest.mark.usefixtures("_setup_env")
    def test_db_import_success_runs_post_db(
        self,
        mock_externals: dict[str, MagicMock],
    ) -> None:
        mock_externals["db_exists"].return_value = False
        mock_externals["ext"].side_effect = lambda point, *_a, **_kw: (
            _a[0] if point == "wt_detect_variant" and _a else True if point == "wt_db_import" else None
        )
        wt_setup(variant="acme")

        ext_calls = [c.args[0] for c in mock_externals["ext"].call_args_list]
        assert "wt_db_import" in ext_calls
        assert "wt_post_db" in ext_calls

    @pytest.mark.usefixtures("_setup_env")
    def test_db_import_failure_skips_post_db(
        self,
        mock_externals: dict[str, MagicMock],
    ) -> None:
        mock_externals["db_exists"].return_value = False
        mock_externals["ext"].side_effect = lambda point, *_a, **_kw: (
            _a[0] if point == "wt_detect_variant" and _a else False if point == "wt_db_import" else None
        )
        wt_setup(variant="acme")

        ext_calls = [c.args[0] for c in mock_externals["ext"].call_args_list]
        assert "wt_db_import" in ext_calls
        assert "wt_post_db" not in ext_calls
