# test-path: cross-cutting — drives `t3 setup` end to end, so it reaches teatree.cli.setup and
# the config resolver the command reads through; neither package alone owns it.
"""``t3 setup`` applies pending teatree self-DB migrations.

A fresh GitHub-origin install has an empty self-DB; ``t3 doctor check`` then
FAILs on the unapplied-migrations gate until they are applied. ``t3 setup`` is
the one command a new user always runs, so it must converge the self-DB to
current — idempotently and quietly when there is nothing to do.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings, resolution
from teatree.config.credential_pass_key import PassKeySource
from teatree.core.overlay import OverlayConfig

#: The two events whose ORDER is the contract — the migrate, and the first read of the
#: ``ConfigSetting`` override tier the migrate creates the table for.
_MIGRATE = "migrate"
_NOTION_ROUTE = "notion-route"
_DB_TIER_READ = "db-tier-read"


def _run_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    migrate_returns: bool = False,
    order: list[str] | None = None,
    notion_route_overlays: dict[str, SimpleNamespace] | None = None,
):
    import typer  # noqa: PLC0415

    from teatree.cli.setup import command as setup_module  # noqa: PLC0415

    skills_src = tmp_path / "core_skills"
    skills_src.mkdir()
    (skills_src / "code").mkdir()
    (skills_src / "code" / "SKILL.md").touch()

    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

    repo = tmp_path / "teatree"
    repo.mkdir()
    (repo / "apm.yml").touch()
    (repo / ".git").mkdir()

    def _migrate(*_args: object, **_kwargs: object) -> bool:
        if order is not None:
            order.append(_MIGRATE)
        return migrate_returns

    def _db_tier_read(*_args: object, **_kwargs: object) -> tuple[dict[str, object], bool]:
        if order is not None:
            order.append(_DB_TIER_READ)
        return {}, False

    def _provision_notion_route() -> int:
        if order is not None:
            order.append(_NOTION_ROUTE)
        return 1

    notion_route_impl = (
        setup_module.provision_declared_notion_routing if notion_route_overlays is not None else _provision_notion_route
    )

    raised: list[int] = []
    with (
        patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
        patch.object(setup_module, "find_main_clone", return_value=repo),
        patch.object(setup_module, "PluginRegistrar"),
        patch.object(setup_module, "ensure_self_db_migrated", side_effect=_migrate) as mock_migrate,
        patch.object(setup_module, "provision_all_overlay_dm_channels"),
        patch.object(
            setup_module,
            "provision_declared_notion_routing",
            side_effect=notion_route_impl,
        ) as mock_notion_route,
        patch("teatree.core.overlay_loader.get_all_overlays", return_value=notion_route_overlays or {}),
        patch.object(setup_module, "seed_default_loops") as mock_seed_loops,
        # The resolver's OWN bindings, not ``override_reader``'s: ``resolution`` binds both
        # names at module level, so patching the defining module reaches neither.
        patch.object(resolution, "load_global_rows", _db_tier_read),
        patch.object(resolution, "load_overlay_rows", _db_tier_read),
        patch("teatree.config.load_config") as mock_load,
    ):
        # A real dataclass, not a MagicMock attribute: the resolver `replace()`s the
        # loaded settings, so a stub that answers any attribute passes nothing it reads on.
        mock_load.return_value.user = UserSettings(workspace_dir=str(tmp_path / "workspace"))
        try:
            setup_module.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)
        except typer.Exit as exc:
            raised.append(exc.exit_code)

    return mock_migrate, mock_notion_route, mock_seed_loops, raised


class TestSetupRunsSelfDbMigrations:
    def test_setup_invokes_self_db_migrate_quietly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_migrate, _mock_notion_route, _mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch)
        mock_migrate.assert_called_once_with(quiet=True)
        assert raised == []

    def test_setup_exits_nonzero_when_self_db_left_unmigrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_migrate, _mock_notion_route, _mock_seed_loops, raised = _run_setup(
            tmp_path, monkeypatch, migrate_returns=True
        )
        assert raised == [1]


class TestSetupSeedsDefaultLoops:
    """``t3 setup`` idempotently seeds the default loops + prompts after the migrate (#2513)."""

    def test_setup_seeds_default_loops_after_a_clean_migrate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_migrate, _mock_notion_route, mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch)
        mock_seed_loops.assert_called_once_with()
        assert raised == []

    def test_setup_skips_loop_seed_when_self_db_left_unmigrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ``Loop`` table may not exist on an unmigrated self-DB, so the loop
        # seed is skipped along with the config seed.
        _mock_migrate, _mock_notion_route, mock_seed_loops, raised = _run_setup(
            tmp_path, monkeypatch, migrate_returns=True
        )
        mock_seed_loops.assert_not_called()
        assert raised == [1]


class TestSetupProvisionsDeclaredNotionRouting:
    def test_setup_provisions_the_route_after_a_clean_migrate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        _mock_migrate, mock_notion_route, _mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch, order=order)

        mock_notion_route.assert_called_once_with()
        assert order.index(_MIGRATE) < order.index(_NOTION_ROUTE)
        assert raised == []

    def test_setup_does_not_touch_the_route_when_migration_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_migrate, mock_notion_route, _mock_seed_loops, raised = _run_setup(
            tmp_path, monkeypatch, migrate_returns=True
        )

        mock_notion_route.assert_not_called()
        assert raised == [1]


class TestSetupPersistsDeclaredNotionRouting(TestCase):
    def test_fresh_setup_persists_the_route_in_the_real_database(self) -> None:
        config = OverlayConfig()
        config._register_secret("notion_token", "store/declared")
        config._pass_key_scope = "acme"
        overlay = SimpleNamespace(config=config)
        monkeypatch = pytest.MonkeyPatch()

        try:
            with TemporaryDirectory() as directory:
                _run_setup(
                    Path(directory),
                    monkeypatch,
                    notion_route_overlays={"acme": overlay},
                )
        finally:
            monkeypatch.undo()

        resolved = config.resolve_pass_key("notion_token")
        assert (resolved.value, resolved.source) == ("store/declared", PassKeySource.OVERLAY_DB)


class TestSelfDbMigrateRunsBeforeTheFirstSettingsRead:
    """The ORDERING is the contract, and nothing but this asserted it (#4585 review).

    ``ConfigSetting`` is the resolver's DB override tier, and a fresh install has no table
    for it until the migrate runs — so a settings read placed above the migrate resolves
    from defaults AND logs a read fault with a traceback, on the one command every new user
    runs. The call site is otherwise held by a comment alone: moving it below the read, or
    adding a read above it, leaves the whole suite green.
    """

    def test_the_migrate_precedes_every_db_override_tier_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        _mock_migrate, _mock_notion_route, _mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch, order=order)
        assert raised == []
        assert order[:1] == [_MIGRATE], f"a settings read ran before the self-DB migrate: {order}"

    def test_the_run_actually_reads_the_override_tier(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Anti-vacuity: an ordering assertion over a log with no read in it holds for free.
        order: list[str] = []
        _run_setup(tmp_path, monkeypatch, order=order)
        assert _DB_TIER_READ in order, f"no DB override-tier read was observed: {order}"
