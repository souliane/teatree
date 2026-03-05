"""Tests for wt_db_refresh.py -- DB refresh/reset workflow."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from wt_db_refresh import wt_db_refresh

pytestmark = pytest.mark.usefixtures("pg_env")


@pytest.fixture
def ctx_and_mocks(
    workspace: Path,
    ticket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, MagicMock]]:
    """Set up env + mock all externals for wt_db_refresh tests."""
    monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
    monkeypatch.chdir(ticket_dir / "my-project")

    # Write .env.worktree so variant detection works
    envwt = ticket_dir / ".env.worktree"
    envwt.write_text("WT_VARIANT=acme\nWT_DB_NAME=wt_1234_acme\n")

    with (
        patch("lib.init.init"),
        patch("wt_db_refresh.ext") as mock_ext,
        patch("wt_db_refresh.db_exists") as mock_db_exists,
        patch("wt_db_refresh.subprocess.run") as mock_run,
    ):
        mock_ext.side_effect = lambda point, *a, **_kw: a[0] if point == "wt_detect_variant" and a else None
        mock_db_exists.return_value = True
        mock_run.return_value = MagicMock(returncode=0)
        yield {
            "ext": mock_ext,
            "db_exists": mock_db_exists,
            "subprocess": mock_run,
        }


class TestWtDbRefreshErrorHandling:
    def test_returns_error_when_resolve_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path / "workspace"))
        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_USER", "testuser")
        monkeypatch.setenv("POSTGRES_PASSWORD", "testpass")
        monkeypatch.chdir(tmp_path)
        with (
            patch("lib.init.init"),
            patch("wt_db_refresh.ext"),
        ):
            result = wt_db_refresh()
        assert result == 1


class TestWtDbRefreshFastPath:
    """Without --force: try DSLR restore first."""

    def test_dslr_restore_succeeds(self, ctx_and_mocks: dict[str, MagicMock]) -> None:
        result = wt_db_refresh(variant="acme")
        assert result == 0

        # Should try DSLR restore
        dslr_calls = [c for c in ctx_and_mocks["subprocess"].call_args_list if "dslr" in c.args[0]]
        assert any("restore" in c.args[0] for c in dslr_calls)

        # Should call wt_post_db after restore
        ext_calls = [c.args[0] for c in ctx_and_mocks["ext"].call_args_list]
        assert "wt_post_db" in ext_calls

    def test_dslr_fails_falls_back_to_import(
        self,
        ctx_and_mocks: dict[str, MagicMock],
    ) -> None:
        # DSLR restore fails
        ctx_and_mocks["subprocess"].return_value = MagicMock(returncode=1)
        # wt_db_import returns True (success)
        ctx_and_mocks["ext"].side_effect = lambda point, *a, **_kw: (
            a[0] if point == "wt_detect_variant" and a else True if point == "wt_db_import" else None
        )

        result = wt_db_refresh(variant="acme")
        assert result == 0

        ext_calls = [c.args[0] for c in ctx_and_mocks["ext"].call_args_list]
        assert "wt_db_import" in ext_calls
        assert "wt_post_db" in ext_calls

    def test_both_fail_returns_error(self, ctx_and_mocks: dict[str, MagicMock]) -> None:
        # DSLR restore fails
        ctx_and_mocks["subprocess"].return_value = MagicMock(returncode=1)
        # wt_db_import also fails
        ctx_and_mocks["ext"].side_effect = lambda point, *a, **_kw: (
            a[0] if point == "wt_detect_variant" and a else False if point == "wt_db_import" else None
        )

        result = wt_db_refresh(variant="acme")
        assert result == 1

    def test_skips_dslr_when_db_does_not_exist(
        self,
        ctx_and_mocks: dict[str, MagicMock],
    ) -> None:
        ctx_and_mocks["db_exists"].return_value = False
        # wt_db_import succeeds
        ctx_and_mocks["ext"].side_effect = lambda point, *a, **_kw: (
            a[0] if point == "wt_detect_variant" and a else True if point == "wt_db_import" else None
        )

        result = wt_db_refresh(variant="acme")
        assert result == 0

        # Should NOT try DSLR restore (DB doesn't exist)
        dslr_restore_calls = [
            c for c in ctx_and_mocks["subprocess"].call_args_list if "dslr" in c.args[0] and "restore" in c.args[0]
        ]
        assert len(dslr_restore_calls) == 0


class TestWtDbResetForce:
    """With --force: drops DB + DSLR snapshot, then reimports."""

    def test_drops_db_before_import(self, ctx_and_mocks: dict[str, MagicMock]) -> None:
        ctx_and_mocks["ext"].side_effect = lambda point, *a, **_kw: (
            a[0] if point == "wt_detect_variant" and a else True if point == "wt_db_import" else None
        )

        result = wt_db_refresh(variant="acme", force=True)
        assert result == 0

        # Should call dropdb
        sub_calls = ctx_and_mocks["subprocess"].call_args_list
        dropdb_calls = [c for c in sub_calls if "dropdb" in c.args[0]]
        assert len(dropdb_calls) == 1
        assert "wt_1234_acme" in dropdb_calls[0].args[0]

    def test_does_not_delete_dslr_snapshot(
        self,
        ctx_and_mocks: dict[str, MagicMock],
    ) -> None:
        """Force mode drops DB but preserves DSLR snapshots and dumps."""
        ctx_and_mocks["ext"].side_effect = lambda point, *a, **_kw: (
            a[0] if point == "wt_detect_variant" and a else True if point == "wt_db_import" else None
        )

        wt_db_refresh(variant="acme", force=True)

        sub_calls = ctx_and_mocks["subprocess"].call_args_list
        dslr_delete_calls = [c for c in sub_calls if "dslr" in c.args[0] and "delete" in c.args[0]]
        assert len(dslr_delete_calls) == 0

    def test_does_not_try_dslr_restore(
        self,
        ctx_and_mocks: dict[str, MagicMock],
    ) -> None:
        """Force mode should skip DSLR restore and go straight to import."""
        ctx_and_mocks["ext"].side_effect = lambda point, *a, **_kw: (
            a[0] if point == "wt_detect_variant" and a else True if point == "wt_db_import" else None
        )

        wt_db_refresh(variant="acme", force=True)

        sub_calls = ctx_and_mocks["subprocess"].call_args_list
        dslr_restore_calls = [c for c in sub_calls if "dslr" in c.args[0] and "restore" in c.args[0]]
        assert len(dslr_restore_calls) == 0

    def test_takes_snapshot_after_import(
        self,
        ctx_and_mocks: dict[str, MagicMock],
    ) -> None:
        ctx_and_mocks["ext"].side_effect = lambda point, *a, **_kw: (
            a[0] if point == "wt_detect_variant" and a else True if point == "wt_db_import" else None
        )

        wt_db_refresh(variant="acme", force=True)

        sub_calls = ctx_and_mocks["subprocess"].call_args_list
        dslr_snapshot_calls = [c for c in sub_calls if "dslr" in c.args[0] and "snapshot" in c.args[0]]
        assert len(dslr_snapshot_calls) == 1

    def test_calls_post_db_after_import(
        self,
        ctx_and_mocks: dict[str, MagicMock],
    ) -> None:
        ctx_and_mocks["ext"].side_effect = lambda point, *a, **_kw: (
            a[0] if point == "wt_detect_variant" and a else True if point == "wt_db_import" else None
        )

        wt_db_refresh(variant="acme", force=True)

        ext_calls = [c.args[0] for c in ctx_and_mocks["ext"].call_args_list]
        assert "wt_post_db" in ext_calls
        # wt_post_db must come AFTER wt_db_import
        import_idx = ext_calls.index("wt_db_import")
        post_idx = ext_calls.index("wt_post_db")
        assert post_idx > import_idx


class TestVariantDetection:
    def test_reads_variant_from_env_worktree(
        self,
        ctx_and_mocks: dict[str, MagicMock],
    ) -> None:
        """When no variant is passed, detect from .env.worktree."""
        # ext returns empty for wt_detect_variant (simulating no arg)
        ctx_and_mocks["ext"].side_effect = lambda point, *_a, **_kw: (
            "" if point == "wt_detect_variant" else True if point == "wt_db_import" else None
        )

        result = wt_db_refresh(variant="")
        assert result == 0

        # Should have used "acme" from .env.worktree for db_name
        ext_calls = ctx_and_mocks["ext"].call_args_list
        db_import_call = [c for c in ext_calls if c.args[0] == "wt_db_import"]
        if db_import_call:
            assert "wt_1234_acme" in db_import_call[0].args

    def test_db_name_without_variant(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without variant, db_name should be just wt_<ticket_number>."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.setenv("POSTGRES_HOST", "localhost")
        monkeypatch.setenv("POSTGRES_USER", "testuser")
        monkeypatch.setenv("POSTGRES_PASSWORD", "testpass")
        monkeypatch.chdir(ticket_dir / "my-project")

        envwt = ticket_dir / ".env.worktree"
        envwt.write_text("WT_VARIANT=\nWT_DB_NAME=wt_1234\n")

        with (
            patch("lib.init.init"),
            patch("wt_db_refresh.ext") as mock_ext,
            patch("wt_db_refresh.db_exists", return_value=False),
            patch("wt_db_refresh.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            mock_ext.side_effect = lambda point, *_a, **_kw: (
                "" if point == "wt_detect_variant" else True if point == "wt_db_import" else None
            )

            wt_db_refresh(variant="")

            db_import_call = [c for c in mock_ext.call_args_list if c.args[0] == "wt_db_import"]
            assert db_import_call
            assert db_import_call[0].args[1] == "wt_1234"
