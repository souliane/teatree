"""Tests for teatree.core.worktree.worktree_env — env cache generation."""

import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.worktree.worktree_env as worktree_env_mod
from teatree.core.models import Ticket, Worktree, WorktreeEnvOverride
from teatree.core.overlay import OverlayBase, OverlayProvisioning, ProvisionStep
from teatree.core.worktree.worktree_env import (
    CACHE_DIRNAME,
    CACHE_FILENAME,
    detect_drift,
    load_overrides,
    render_env_cache,
    set_override,
    worktree_pg_connection,
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


class _SharedPostgresOverlay_Provisioning(OverlayProvisioning):
    def db_import_strategy(self, worktree: Worktree) -> DbImportStrategy:
        return DbImportStrategy(shared_postgres=True)


class SharedPostgresOverlay(OverlayBase):
    provisioning = _SharedPostgresOverlay_Provisioning()
    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []



class _EnvExtraOverrideOverlay_Provisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"WT_DB_NAME": "custom_db", "EXTRA_KEY": "extra_value"}

    def declared_env_keys(self) -> set[str]:
        return {"WT_DB_NAME", "EXTRA_KEY"}


class EnvExtraOverrideOverlay(OverlayBase):
    provisioning = _EnvExtraOverrideOverlay_Provisioning()
    """Overlay whose provisioning.env_extra collides with a core key."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []




class _ExtraOnlyOverlay_Provisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"EXTRA_KEY": "extra_value"}

    def declared_env_keys(self) -> set[str]:
        return {"EXTRA_KEY"}


class ExtraOnlyOverlay(OverlayBase):
    provisioning = _ExtraOnlyOverlay_Provisioning()
    """Overlay that declares a non-colliding extra key."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []




class _PostgresPasswordOverlay_Provisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"POSTGRES_PASSWORD": "s3cr3t-pw", "EXTRA_KEY": "extra_value"}

    def declared_env_keys(self) -> set[str]:
        return {"POSTGRES_PASSWORD", "EXTRA_KEY"}


class PostgresPasswordOverlay(OverlayBase):
    provisioning = _PostgresPasswordOverlay_Provisioning()
    """Overlay that returns POSTGRES_PASSWORD without declaring it secret."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []




class _SecretOverlay_Provisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {
            "PUBLIC_KEY": "public_value",
            "SECRET_PASSWORD": "s3cr3t",
            "DATABASE_URL": "postgresql://u:s3cr3t@host/db",
        }

    def declared_env_keys(self) -> set[str]:
        return {"PUBLIC_KEY", "SECRET_PASSWORD", "DATABASE_URL"}

    def declared_secret_env_keys(self) -> set[str]:
        return {"SECRET_PASSWORD", "DATABASE_URL"}


class SecretOverlay(OverlayBase):
    provisioning = _SecretOverlay_Provisioning()
    """Overlay whose provisioning.env_extra includes both public and secret keys."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []





class _BaseImageOverlay_Provisioning(OverlayProvisioning):
    def __init__(self, overlay: "BaseImageOverlay") -> None:
        self._overlay = overlay

    def base_images(self, worktree: Worktree) -> list[BaseImageConfig]:
        return [
            BaseImageConfig(
                image_name="myapp-local",
                dockerfile="Dockerfile-local",
                lockfile="Pipfile.lock",
                build_context=self._overlay._context,
                env_var="MYAPP_BASE_IMAGE",
            )
        ]


class BaseImageOverlay(OverlayBase):
    """Overlay that declares a base image — tag should land in env cache."""

    def __init__(self, context: Path) -> None:
        super().__init__()
        self._context = context
        self.provisioning = _BaseImageOverlay_Provisioning(self)

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []



_SHARED_PG = {"test": SharedPostgresOverlay()}
_ENV_EXTRA_COLLIDES = {"test": EnvExtraOverrideOverlay()}
_EXTRA_ONLY = {"test": ExtraOnlyOverlay()}
_SECRET = {"test": SecretOverlay()}
_POSTGRES_PW = {"test": PostgresPasswordOverlay()}
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

    def test_core_filters_postgres_password_without_overlay_declaring_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="tp", ticket_url="https://ex.com/2", variant="acme")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_POSTGRES_PW):
                spec = render_env_cache(wt)
            assert spec is not None
            assert "EXTRA_KEY=extra_value" in spec.content
            assert "POSTGRES_PASSWORD=" not in spec.content
            assert "s3cr3t-pw" not in spec.content
            # The symbolic pass-key reference replaces the stripped literal.
            assert "POSTGRES_PASSWORD_PASS_KEY=teatree/wt/" in spec.content


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

            # Real file in repo — not a symlink (#1313). A symlink would dangle
            # inside a bind-mounted container because the target is a host-only
            # absolute path.
            repo_copy = wt_path / CACHE_FILENAME
            assert not repo_copy.is_symlink()
            assert repo_copy.is_file()
            assert repo_copy.read_text(encoding="utf-8") == spec.path.read_text(encoding="utf-8")

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
            assert "MYAPP_BASE_IMAGE=myapp-local:base" in spec.content


class TestRepoCopyNotSymlink(TestCase):
    """Regression for #1313 — the in-worktree copy must be a real file.

    A symlink at ``<wt_path>/.t3-env.cache`` pointing at the host-absolute
    cache path dangles inside a bind-mounted container, breaking any
    in-container reader of the env file (pipenv, dotenv, plain ``stat``)
    with errno 22.
    """

    def test_repo_copy_is_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, wt_path = _make_worktree(tmp, ticket_name="t-r1", ticket_url="https://ex.com/r1", db_name="wt_r1")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)
            assert spec is not None
            repo_copy = wt_path / CACHE_FILENAME
            assert not repo_copy.is_symlink()
            assert repo_copy.is_file()

    def test_repo_copy_content_equals_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, wt_path = _make_worktree(tmp, ticket_name="t-r2", ticket_url="https://ex.com/r2", db_name="wt_r2")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)
            assert spec is not None
            repo_copy = wt_path / CACHE_FILENAME
            assert repo_copy.read_text(encoding="utf-8") == spec.path.read_text(encoding="utf-8")

    def test_repo_copy_survives_source_deletion(self) -> None:
        """A real file is independent of the source — symlinks would dangle."""
        with tempfile.TemporaryDirectory() as tmp:
            wt, wt_path = _make_worktree(tmp, ticket_name="t-r3", ticket_url="https://ex.com/r3", db_name="wt_r3")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)
            assert spec is not None
            repo_copy = wt_path / CACHE_FILENAME
            expected = spec.path.read_text(encoding="utf-8")
            spec.path.chmod(stat.S_IWUSR | stat.S_IRUSR)
            spec.path.unlink()
            assert repo_copy.is_file()
            assert not repo_copy.is_symlink()
            assert repo_copy.read_text(encoding="utf-8") == expected


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

    def test_render_includes_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t", ticket_url="https://ex.com/1", db_name="wt_1")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                set_override(wt, "MY_KEY", "my_value")
                spec = render_env_cache(wt)
            assert spec is not None
            assert "MY_KEY" in spec.keys
            assert "MY_KEY=my_value" in spec.content

    def test_override_wins_over_overlay_extra(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="t", ticket_url="https://ex.com/1", db_name="wt_1")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_EXTRA_ONLY):
                WorktreeEnvOverride.objects.create(worktree=wt, key="EXTRA_KEY", value="overridden")
                spec = render_env_cache(wt)
            assert spec is not None
            assert "EXTRA_KEY=overridden" in spec.content
            assert "EXTRA_KEY=extra_value" not in spec.content

    def test_override_of_secret_key_still_dropped_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="ts", ticket_url="https://ex.com/1", variant="acme")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_SECRET):
                WorktreeEnvOverride.objects.create(worktree=wt, key="SECRET_PASSWORD", value="leak")
                spec = render_env_cache(wt)
            assert spec is not None
            assert "SECRET_PASSWORD" not in spec.content
            assert "leak" not in spec.content


class _PgUserOverlay_Provisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"POSTGRES_USER": "db_superuser", "POSTGRES_HOST": "db.internal", "POSTGRES_PORT": "5544"}


class _PgUserOverlay(OverlayBase):
    provisioning = _PgUserOverlay_Provisioning()
    """Overlay that connects to postgres as a non-default role + custom port."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []



_PG_USER = {"test": _PgUserOverlay()}


class TestWorktreePgConnection(TestCase):
    def test_resolves_overlay_role_host_and_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, _ = _make_worktree(tmp, ticket_name="pgc", ticket_url="https://ex.com/pgc", db_name="wt_pgc")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_PG_USER):
                user, host, env = worktree_pg_connection(wt)
        assert user == "db_superuser"
        assert host == "db.internal"
        assert env["PGPORT"] == "5544"

    def test_unprovisioned_worktree_returns_blank_defaults(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://ex.com/np")
        wt = Worktree.objects.create(
            overlay="test", ticket=ticket, repo_path="backend", branch="feature", db_name="wt_np", extra={}
        )
        user, host, env = worktree_pg_connection(wt)
        assert (user, host, env) == ("", "", {})
