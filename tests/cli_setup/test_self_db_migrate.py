"""``t3 setup`` applies pending teatree self-DB migrations.

A fresh GitHub-origin install has an empty self-DB; ``t3 doctor check`` then
FAILs on the unapplied-migrations gate until they are applied. ``t3 setup`` is
the one command a new user always runs, so it must converge the self-DB to
current — idempotently and quietly when there is nothing to do.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _run_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, migrate_returns: bool = False):
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

    raised: list[int] = []
    with (
        patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
        patch.object(setup_module, "find_main_clone", return_value=repo),
        patch.object(setup_module, "ApmInstaller"),
        patch.object(setup_module, "PluginRegistrar"),
        patch.object(setup_module, "ensure_self_db_migrated", return_value=migrate_returns) as mock_migrate,
        patch.object(setup_module, "seed_db_config_from_toml") as mock_seed,
        patch.object(setup_module, "seed_default_loops") as mock_seed_loops,
        patch("teatree.config.load_config") as mock_load,
    ):
        mock_load.return_value.user.contribute = False
        mock_load.return_value.user.excluded_skills = []
        mock_load.return_value.user.workspace_dir = str(tmp_path / "workspace")
        try:
            setup_module.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)
        except typer.Exit as exc:
            raised.append(exc.exit_code)

    return mock_migrate, mock_seed, mock_seed_loops, raised


class TestSetupRunsSelfDbMigrations:
    def test_setup_invokes_self_db_migrate_quietly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_migrate, _mock_seed, _mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch)
        mock_migrate.assert_called_once_with(quiet=True)
        assert raised == []

    def test_setup_exits_nonzero_when_self_db_left_unmigrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_migrate, _mock_seed, _mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch, migrate_returns=True)
        assert raised == [1]


class TestSetupSeedsDbConfigFromToml:
    """``t3 setup`` runs the #938 dual-read auto-migration after the self-DB migrate (TODO-75)."""

    def test_setup_seeds_db_config_after_a_clean_migrate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_migrate, mock_seed, _mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch)
        mock_seed.assert_called_once_with()
        assert raised == []

    def test_setup_skips_seed_when_self_db_left_unmigrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ``ConfigSetting`` table may not exist on an unmigrated self-DB, so the
        # seed is skipped — the setup still exits non-zero on the unmigrated DB.
        _mock_migrate, mock_seed, _mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch, migrate_returns=True)
        mock_seed.assert_not_called()
        assert raised == [1]


class TestSetupSeedsDefaultLoops:
    """``t3 setup`` idempotently seeds the default loops + prompts after the migrate (#2513)."""

    def test_setup_seeds_default_loops_after_a_clean_migrate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_migrate, _mock_seed, mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch)
        mock_seed_loops.assert_called_once_with()
        assert raised == []

    def test_setup_skips_loop_seed_when_self_db_left_unmigrated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ``Loop`` table may not exist on an unmigrated self-DB, so the loop
        # seed is skipped along with the config seed.
        _mock_migrate, _mock_seed, mock_seed_loops, raised = _run_setup(tmp_path, monkeypatch, migrate_returns=True)
        mock_seed_loops.assert_not_called()
        assert raised == [1]
