"""Tests for teatree.core.worktree_env — .env.worktree generation."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.worktree_env as worktree_env_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep
from teatree.types import DbImportStrategy
from tests.teatree_core.conftest import CommandOverlay


class TestDockerHostAddress(TestCase):
    def test_returns_host_docker_internal_on_darwin(self) -> None:
        with patch.object(worktree_env_mod.platform, "system", return_value="Darwin"):
            assert worktree_env_mod._docker_host_address() == "host.docker.internal"

    def test_returns_host_docker_internal_on_windows(self) -> None:
        with patch.object(worktree_env_mod.platform, "system", return_value="Windows"):
            assert worktree_env_mod._docker_host_address() == "host.docker.internal"

    def test_returns_bridge_gateway_on_linux(self) -> None:
        with patch.object(worktree_env_mod.platform, "system", return_value="Linux"):
            assert worktree_env_mod._docker_host_address() == "172.17.0.1"


class SharedPostgresOverlay(OverlayBase):
    """Overlay that declares shared_postgres in its DB import strategy."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy:
        return DbImportStrategy(shared_postgres=True)


class EnvExtraOverrideOverlay(OverlayBase):
    """Overlay that provides env extras overriding a core key."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"WT_DB_NAME": "custom_db", "EXTRA_KEY": "extra_value"}


_SHARED_PG = {"test": SharedPostgresOverlay()}
_ENV_EXTRA = {"test": EnvExtraOverrideOverlay()}
_COMMAND = {"test": CommandOverlay()}


class TestWriteEnvWorktree(TestCase):
    def test_returns_none_when_no_worktree_path(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")
        wt = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={},
        )
        assert worktree_env_mod.write_env_worktree(wt) is None

    def test_writes_basic_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ticket-1"
            ticket_dir.mkdir()
            wt_path = ticket_dir / "backend"
            wt_path.mkdir()

            ticket = Ticket.objects.create(
                overlay="test",
                issue_url="https://example.com/issues/42",
                variant="acme",
            )
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="wt_42_acme",
                extra={"worktree_path": str(wt_path)},
            )

            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                result = worktree_env_mod.write_env_worktree(wt)

            assert result is not None
            envfile = Path(result)
            assert envfile.exists()
            content = envfile.read_text(encoding="utf-8")
            assert "WT_VARIANT=acme" in content
            assert f"TICKET_DIR={ticket_dir}" in content
            assert "TICKET_URL=https://example.com/issues/42" in content
            assert "WT_DB_NAME=wt_42_acme" in content
            assert f"COMPOSE_PROJECT_NAME=backend-wt{ticket.ticket_number}" in content

            # Symlink created in repo worktree dir
            repo_envwt = wt_path / ".env.worktree"
            assert repo_envwt.is_symlink()
            assert repo_envwt.resolve() == envfile.resolve()

    def test_shared_postgres_adds_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ticket-2"
            ticket_dir.mkdir()
            wt_path = ticket_dir / "backend"
            wt_path.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="wt_2",
                extra={"worktree_path": str(wt_path)},
            )

            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_SHARED_PG),
                patch.object(worktree_env_mod.platform, "system", return_value="Darwin"),
            ):
                result = worktree_env_mod.write_env_worktree(wt)

            assert result is not None
            content = Path(result).read_text(encoding="utf-8")
            assert "POSTGRES_HOST=host.docker.internal" in content

    def test_env_extra_overrides_core_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ticket-3"
            ticket_dir.mkdir()
            wt_path = ticket_dir / "backend"
            wt_path.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/3")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="wt_3",
                extra={"worktree_path": str(wt_path)},
            )

            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_ENV_EXTRA):
                result = worktree_env_mod.write_env_worktree(wt)

            assert result is not None
            content = Path(result).read_text(encoding="utf-8")
            # WT_DB_NAME should be overridden by overlay's env_extra
            assert "WT_DB_NAME=custom_db" in content
            # Should not have the original value
            assert "WT_DB_NAME=wt_3" not in content
            # New key should be appended
            assert "EXTRA_KEY=extra_value" in content

    def test_overwrites_existing_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ticket-4"
            ticket_dir.mkdir()
            wt_path = ticket_dir / "backend"
            wt_path.mkdir()

            # Create a pre-existing symlink
            repo_envwt = wt_path / ".env.worktree"
            dummy = ticket_dir / "old_env"
            dummy.write_text("old")
            repo_envwt.symlink_to(dummy)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/4")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="wt_4",
                extra={"worktree_path": str(wt_path)},
            )

            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                result = worktree_env_mod.write_env_worktree(wt)

            assert result is not None
            # Symlink should now point to the new envfile, not the old one
            assert repo_envwt.is_symlink()
            assert repo_envwt.resolve() != dummy.resolve()
