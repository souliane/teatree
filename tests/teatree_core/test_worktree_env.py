"""Tests for teatree.core.worktree_env — env cache generation."""

import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.worktree_env as worktree_env_mod
from teatree.core.models import Ticket, Worktree, WorktreeEnvOverride
from teatree.core.overlay import OverlayBase, ProvisionStep
from teatree.core.worktree_env import (
    CACHE_DIRNAME,
    CACHE_FILENAME,
    detect_drift,
    load_overrides,
    render_env_cache,
    set_override,
    write_env_cache,
)
from teatree.types import BaseImageConfig, DbImportStrategy
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
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_db_import_strategy(self, worktree: Worktree) -> DbImportStrategy:
        return DbImportStrategy(shared_postgres=True)


class EnvExtraOverrideOverlay(OverlayBase):
    """Overlay whose get_env_extra collides with a core key."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"WT_DB_NAME": "custom_db", "EXTRA_KEY": "extra_value"}

    def declared_env_keys(self) -> set[str]:
        return {"WT_DB_NAME", "EXTRA_KEY"}


class ExtraOnlyOverlay(OverlayBase):
    """Overlay that declares a non-colliding extra key."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"EXTRA_KEY": "extra_value"}

    def declared_env_keys(self) -> set[str]:
        return {"EXTRA_KEY"}


class SecretOverlay(OverlayBase):
    """Overlay whose get_env_extra includes both public and secret keys."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {
            "PUBLIC_KEY": "public_value",
            "SECRET_PASSWORD": "s3cr3t",
            "DATABASE_URL": "postgresql://u:s3cr3t@host/db",
        }

    def declared_env_keys(self) -> set[str]:
        return {"PUBLIC_KEY", "SECRET_PASSWORD", "DATABASE_URL"}

    def declared_secret_env_keys(self) -> set[str]:
        return {"SECRET_PASSWORD", "DATABASE_URL"}


class BaseImageOverlay(OverlayBase):
    """Overlay that declares a base image — tag should land in env cache."""

    def __init__(self, context: Path) -> None:
        super().__init__()
        self._context = context

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []

    def get_base_images(self, worktree: Worktree) -> list[BaseImageConfig]:
        return [
            BaseImageConfig(
                image_name="myapp-local",
                dockerfile="Dockerfile-local",
                lockfile="Pipfile.lock",
                build_context=self._context,
                env_var="MYAPP_BASE_IMAGE",
            )
        ]


_SHARED_PG = {"test": SharedPostgresOverlay()}
_ENV_EXTRA_COLLIDES = {"test": EnvExtraOverrideOverlay()}
_EXTRA_ONLY = {"test": ExtraOnlyOverlay()}
_SECRET = {"test": SecretOverlay()}
_COMMAND = {"test": CommandOverlay()}


def _make_worktree(
    tmp: str,
    *,
    ticket_name: str,
    ticket_url: str,
    db_name: str = "wt_1",
    **ticket_kwargs: object,
) -> tuple[Worktree, Path]:
    ticket_dir = Path(tmp) / ticket_name
    ticket_dir.mkdir()
    wt_path = ticket_dir / "backend"
    wt_path.mkdir()
    ticket = Ticket.objects.create(overlay="test", issue_url=ticket_url, **ticket_kwargs)
    wt = Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        db_name=db_name,
        extra={"worktree_path": str(wt_path)},
    )
    return wt, wt_path


class TestRenderEnvCache(TestCase):
    def test_returns_none_when_no_worktree_path(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")
        wt = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={},
        )
        assert render_env_cache(wt) is None

    def test_raises_when_overlay_collides_with_core(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t", ticket_url="https://ex.com/1", variant="acme")
            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_ENV_EXTRA_COLLIDES),
                pytest.raises(RuntimeError, match="core already owns"),
            ):
                render_env_cache(wt)

    def test_drops_keys_in_declared_secret_env_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="ts", ticket_url="https://ex.com/1", variant="acme")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_SECRET):
                spec = render_env_cache(wt)
            assert spec is not None
            assert "PUBLIC_KEY=public_value" in spec.content
            assert "SECRET_PASSWORD" not in spec.content
            assert "DATABASE_URL" not in spec.content
            assert "s3cr3t" not in spec.content
            assert "PUBLIC_KEY" in spec.keys
            assert "SECRET_PASSWORD" not in spec.keys
            assert "DATABASE_URL" not in spec.keys


class TestWriteEnvCache(TestCase):
    def test_writes_cache_in_hidden_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, wt_path = _make_worktree(
                tmp,
                ticket_name="ticket-1",
                ticket_url="https://example.com/issues/42",
                variant="acme",
                db_name="wt_42_acme",
            )

            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)

            assert spec is not None
            assert spec.path.name == CACHE_FILENAME
            assert spec.path.parent.name == CACHE_DIRNAME
            content = spec.path.read_text(encoding="utf-8")
            assert "# GENERATED" in content
            assert "WT_VARIANT=acme" in content
            assert "WT_DB_NAME=wt_42_acme" in content

            # chmod 444
            mode = stat.S_IMODE(spec.path.stat().st_mode)
            assert mode == 0o444

            # Symlink in repo
            repo_link = wt_path / CACHE_FILENAME
            assert repo_link.is_symlink()
            assert repo_link.resolve() == spec.path.resolve()

    def test_shared_postgres_adds_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t2", ticket_url="https://ex.com/2", db_name="wt_2")
            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_SHARED_PG),
                patch.object(worktree_env_mod.platform, "system", return_value="Darwin"),
            ):
                spec = write_env_cache(wt)
            assert spec is not None
            assert "POSTGRES_HOST=host.docker.internal" in spec.content

    def test_extra_non_colliding_key_lands_in_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t3", ticket_url="https://ex.com/3", db_name="wt_3")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_EXTRA_ONLY):
                spec = write_env_cache(wt)
            assert spec is not None
            assert "EXTRA_KEY=extra_value" in spec.content

    def test_regenerate_overwrites_readonly_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t4", ticket_url="https://ex.com/4", db_name="wt_4")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                first = write_env_cache(wt)
                assert first is not None
                # File is 444 — the second call must strip, write, re-chmod.
                second = write_env_cache(wt)
            assert second is not None
            assert first.path == second.path
            assert stat.S_IMODE(second.path.stat().st_mode) == 0o444

    def test_emits_redis_db_index_when_allocated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(
                tmp,
                ticket_name="t5",
                ticket_url="https://ex.com/5",
                redis_db_index=7,
                db_name="wt_5",
            )
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)
            assert spec is not None
            assert "REDIS_DB_INDEX=7" in spec.content

    def test_omits_redis_db_index_when_unallocated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t6", ticket_url="https://ex.com/6", db_name="wt_6")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)
            assert spec is not None
            assert "REDIS_DB_INDEX" not in spec.content

    def test_base_image_env_var_lands_in_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t7", ticket_url="https://ex.com/7", db_name="wt_7")
            main_repo = Path(tmp) / "main-repo"
            main_repo.mkdir()
            (main_repo / "Dockerfile-local").write_text("FROM scratch\n")
            (main_repo / "Pipfile.lock").write_text('{"_meta": {"hash": "deadbeef"}}\n')
            overlay = {"test": BaseImageOverlay(context=main_repo)}
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=overlay):
                spec = write_env_cache(wt)
            assert spec is not None
            assert "MYAPP_BASE_IMAGE=myapp-local:deps-" in spec.content


class TestDetectDrift(TestCase):
    def test_no_drift_when_freshly_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t", ticket_url="https://ex.com/1", db_name="wt_1")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                write_env_cache(wt)
                drifted, _ = detect_drift(wt)
            assert drifted is False

    def test_drift_when_file_edited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t", ticket_url="https://ex.com/1", db_name="wt_1")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)
                assert spec is not None
                spec.path.chmod(0o644)
                spec.path.write_text("tampered\n", encoding="utf-8")
                drifted, cache_path = detect_drift(wt)
            assert drifted is True
            assert cache_path == spec.path

    def test_drift_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t", ticket_url="https://ex.com/1", db_name="wt_1")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                drifted, cache_path = detect_drift(wt)
            assert drifted is True
            assert cache_path is not None
            assert cache_path.name == CACHE_FILENAME


class TestOverrides(TestCase):
    def test_set_override_persists_and_regenerates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t", ticket_url="https://ex.com/1", db_name="wt_1")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                set_override(wt, "MY_KEY", "my_value")
                spec = render_env_cache(wt)
            assert WorktreeEnvOverride.objects.filter(worktree=wt, key="MY_KEY").exists()
            assert spec is not None
            assert load_overrides(wt) == {"MY_KEY": "my_value"}

    def test_set_override_rejects_core_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t", ticket_url="https://ex.com/1", db_name="wt_1")
            with pytest.raises(ValueError, match="owned by core"):
                set_override(wt, "WT_DB_NAME", "hack")
