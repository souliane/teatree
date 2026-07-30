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
    env_cache_path,
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


class _SharedPostgresOverlayProvisioning(OverlayProvisioning):
    def db_import_strategy(self, worktree: Worktree) -> DbImportStrategy:
        return DbImportStrategy(shared_postgres=True)


class SharedPostgresOverlay(OverlayBase):
    provisioning = _SharedPostgresOverlayProvisioning()

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class _EnvExtraOverrideOverlayProvisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"WT_DB_NAME": "custom_db", "EXTRA_KEY": "extra_value"}

    def declared_env_keys(self) -> set[str]:
        return {"WT_DB_NAME", "EXTRA_KEY"}


class EnvExtraOverrideOverlay(OverlayBase):
    provisioning = _EnvExtraOverrideOverlayProvisioning()
    """Overlay whose provisioning.env_extra collides with a core key."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class _ExtraOnlyOverlayProvisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"EXTRA_KEY": "extra_value"}

    def declared_env_keys(self) -> set[str]:
        return {"EXTRA_KEY"}


class ExtraOnlyOverlay(OverlayBase):
    provisioning = _ExtraOnlyOverlayProvisioning()
    """Overlay that declares a non-colliding extra key."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class _PostgresPasswordOverlayProvisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"POSTGRES_PASSWORD": "s3cr3t-pw", "EXTRA_KEY": "extra_value"}

    def declared_env_keys(self) -> set[str]:
        return {"POSTGRES_PASSWORD", "EXTRA_KEY"}


class PostgresPasswordOverlay(OverlayBase):
    provisioning = _PostgresPasswordOverlayProvisioning()
    """Overlay that returns POSTGRES_PASSWORD without declaring it secret."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class _SecretOverlayProvisioning(OverlayProvisioning):
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
    provisioning = _SecretOverlayProvisioning()
    """Overlay whose provisioning.env_extra includes both public and secret keys."""

    def get_repos(self) -> list[str]:
        return ["backend"]

    def get_provision_steps(self, worktree: Worktree) -> list[ProvisionStep]:
        return []


class _BaseImageOverlayProvisioning(OverlayProvisioning):
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
        self.provisioning = _BaseImageOverlayProvisioning(self)

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
            # Per repo, under the out-of-repo .t3-cache/ sibling.
            assert spec.path.parent.name == wt_path.name
            assert spec.path.parent.parent.name == CACHE_DIRNAME
            content = spec.path.read_text(encoding="utf-8")
            assert "# GENERATED" in content
            assert "WT_VARIANT=acme" in content
            assert "WT_DB_NAME=wt_42_acme" in content

            # chmod 444
            mode = stat.S_IMODE(spec.path.stat().st_mode)
            assert mode == 0o444

            # No copy inside the repo working tree (#3097) — the cache is the
            # sole out-of-repo file under ``.t3-cache/``.
            assert not (wt_path / CACHE_FILENAME).exists()

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


class TestNoRepoWorkingTreeCopy(TestCase):
    """Regression for #3097 — the cache must not be copied into a repo tree.

    A generated file inside a repo working tree surfaces as untracked in any
    sibling repo whose ignore file does not list it, and an agent staging
    everything can commit it onto a merge request. The single copy lives in
    the out-of-repo ``.t3-cache/`` sibling of the worktree (the #3096
    principle); consumers read it from there.
    """

    def test_cache_not_written_inside_repo_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, wt_path = _make_worktree(tmp, ticket_name="t-r1", ticket_url="https://ex.com/r1", db_name="wt_r1")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)
            assert spec is not None
            assert not (wt_path / CACHE_FILENAME).exists()
            assert list(wt_path.iterdir()) == []

    def test_canonical_cache_lives_outside_the_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, wt_path = _make_worktree(tmp, ticket_name="t-r2", ticket_url="https://ex.com/r2", db_name="wt_r2")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                spec = write_env_cache(wt)
            assert spec is not None
            assert wt_path not in spec.path.parents
            assert spec.path == wt_path.parent / CACHE_DIRNAME / wt_path.name / CACHE_FILENAME
            assert spec.path.is_file()

    def test_stale_in_worktree_copy_is_removed_on_regenerate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt, wt_path = _make_worktree(tmp, ticket_name="t-r3", ticket_url="https://ex.com/r3", db_name="wt_r3")
            stale = wt_path / CACHE_FILENAME
            stale.write_text("leftover from a pre-#3097 provision\n", encoding="utf-8")
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                write_env_cache(wt)
            assert not stale.exists()


def _make_sibling_worktree(ticket: Ticket, ticket_dir: Path, repo: str, db_name: str) -> tuple[Worktree, Path]:
    wt_path = ticket_dir / repo
    wt_path.mkdir()
    wt = Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path=repo,
        branch="feature",
        db_name=db_name,
        extra={"worktree_path": str(wt_path)},
    )
    return wt, wt_path


class TestPerRepoEnvCache(TestCase):
    """Sibling repos of one ticket each get their own env cache (#3097 follow-up).

    #3097 moved the cache out of every repo working tree, but derived its
    path from ``ticket_dir`` alone — identical for every repo sharing the
    ticket dir (the standard multi-repo layout ``ticket_dir/backend``,
    ``ticket_dir/frontend``). The second repo's write then clobbered the
    first, so the non-last-writer's direnv/shell sourced the wrong repo's
    ``COMPOSE_PROJECT_NAME``. The cache must be per repo AND out of every
    repo tree.

    RED before the fix: both repos resolve to one shared path, so the
    backend cache carries the frontend's ``COMPOSE_PROJECT_NAME``.
    """

    def test_sibling_repos_get_distinct_cache_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ac-42-ticket"
            ticket_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://ex.com/42")
            backend, backend_path = _make_sibling_worktree(ticket, ticket_dir, "backend", "wt_be")
            frontend, frontend_path = _make_sibling_worktree(ticket, ticket_dir, "frontend", "wt_fe")

            be_cache = env_cache_path(backend)
            fe_cache = env_cache_path(frontend)

            assert be_cache != fe_cache
            # Out of every repo working tree (the #3097 guarantee).
            assert backend_path not in be_cache.parents
            assert frontend_path not in fe_cache.parents
            # Both under the one shared out-of-repo .t3-cache/ sibling.
            assert be_cache.parents[1] == ticket_dir / CACHE_DIRNAME
            assert fe_cache.parents[1] == ticket_dir / CACHE_DIRNAME

    def test_each_repo_cache_holds_its_own_compose_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket_dir = Path(tmp) / "ac-42-ticket"
            ticket_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://ex.com/42")
            backend, _ = _make_sibling_worktree(ticket, ticket_dir, "backend", "wt_be")
            frontend, _ = _make_sibling_worktree(ticket, ticket_dir, "frontend", "wt_fe")

            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_COMMAND):
                write_env_cache(backend)
                # Provisioning the sibling second must NOT clobber the first.
                write_env_cache(frontend)

            be_content = env_cache_path(backend).read_text(encoding="utf-8")
            fe_content = env_cache_path(frontend).read_text(encoding="utf-8")

            assert f"COMPOSE_PROJECT_NAME=backend-wt{ticket.pk}" in be_content
            assert f"COMPOSE_PROJECT_NAME=frontend-wt{ticket.pk}" in fe_content
            # The last writer never leaks its project into the sibling's cache.
            assert f"COMPOSE_PROJECT_NAME=frontend-wt{ticket.pk}" not in be_content


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


class _PgUserOverlayProvisioning(OverlayProvisioning):
    def env_extra(self, worktree: Worktree) -> dict[str, str]:
        return {"POSTGRES_USER": "db_superuser", "POSTGRES_HOST": "db.internal", "POSTGRES_PORT": "5544"}


class _PgUserOverlay(OverlayBase):
    provisioning = _PgUserOverlayProvisioning()
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


class TestOrmImportsAreDeferred(TestCase):
    """The CLI cold-path refactor keeps ORM/model imports function-level.

    worktree_env must be importable before ``django.setup()`` (bare ``t3``),
    so the model imports are deferred inside the functions rather than bound at
    module top level. A regression that re-binds them at import time silently
    breaks the cold path this refactor exists to protect.
    """

    def test_model_names_are_not_bound_at_module_top_level(self) -> None:
        assert not hasattr(worktree_env_mod, "WorktreeEnvOverride")
        assert not hasattr(worktree_env_mod, "Worktree")

    def test_public_helpers_stay_module_level_callables(self) -> None:
        for fn in (render_env_cache, write_env_cache, detect_drift, env_cache_path, load_overrides, set_override):
            assert callable(fn)
            assert fn.__module__ == "teatree.core.worktree.worktree_env"
