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


def _run_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from teatree.cli import setup as setup_module  # noqa: PLC0415

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

    with (
        patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
        patch.object(setup_module, "_find_main_clone", return_value=repo),
        patch.object(setup_module, "_run_apm_install", return_value=True),
        patch.object(setup_module, "_install_claude_plugin", return_value=True),
        patch.object(setup_module, "ensure_self_db_migrated", return_value=False) as mock_migrate,
        patch("teatree.config.load_config") as mock_load,
    ):
        mock_load.return_value.user.contribute = False
        mock_load.return_value.user.excluded_skills = []
        mock_load.return_value.user.workspace_dir = str(tmp_path / "workspace")
        setup_module.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)

    return mock_migrate


class TestSetupRunsSelfDbMigrations:
    def test_setup_invokes_self_db_migrate_quietly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_migrate = _run_setup(tmp_path, monkeypatch)
        mock_migrate.assert_called_once_with(quiet=True)
