"""Tests for t3 setup skill-symlink synchronization and exclusion.

Lifted verbatim from the former monolithic ``tests/test_cli_setup.py``
(souliane/teatree#443). No behavior change: same assertions, only
relocated under a focused package by concern.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from teatree.cli.setup.skill_linker import CORE_EXCLUDED_SKILLS, SkillLinker, _ensure_skill_link


class TestRemoveExcludedSkills:
    def test_removes_symlinks(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "using-superpowers"
        link.symlink_to(target)
        assert SkillLinker(tmp_path, tmp_path).remove_excluded(["using-superpowers"]) == 1
        assert not link.exists()

    def test_removes_directories(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "using-git-worktrees"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").touch()
        assert SkillLinker(tmp_path, tmp_path).remove_excluded(["using-git-worktrees"]) == 1
        assert not skill_dir.exists()

    def test_ignores_nonexistent(self, tmp_path: Path) -> None:
        assert SkillLinker(tmp_path, tmp_path).remove_excluded(["nonexistent"]) == 0

    def test_multiple_excluded(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        (tmp_path / "skill-a").symlink_to(target)
        (tmp_path / "skill-b").mkdir()
        assert SkillLinker(tmp_path, tmp_path).remove_excluded(["skill-a", "skill-b", "skill-c"]) == 2

    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        assert SkillLinker(tmp_path, tmp_path).remove_excluded(["../etc", ".hidden"]) == 0

    def test_rejects_nested_path(self, tmp_path: Path) -> None:
        assert SkillLinker(tmp_path, tmp_path).remove_excluded(["foo/bar"]) == 0


class TestEnsureSkillLink:
    def test_creates_new_link(self, tmp_path: Path) -> None:
        target = tmp_path / "source"
        target.mkdir()
        link = tmp_path / "link"
        created, fixed = _ensure_skill_link(target, link, tmp_path / "workspace")
        assert created == 1
        assert fixed == 0
        assert link.is_symlink()
        assert link.resolve() == target.resolve()

    def test_leaves_correct_link(self, tmp_path: Path) -> None:
        target = tmp_path / "source"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        created, fixed = _ensure_skill_link(target, link, tmp_path / "workspace")
        assert created == 0
        assert fixed == 0

    def test_fixes_wrong_link(self, tmp_path: Path) -> None:
        target = tmp_path / "source"
        target.mkdir()
        wrong = tmp_path / "wrong"
        wrong.mkdir()
        link = tmp_path / "link"
        link.symlink_to(wrong)
        created, fixed = _ensure_skill_link(target, link, tmp_path / "workspace")
        assert created == 0
        assert fixed == 1
        assert link.resolve() == target.resolve()

    def test_preserves_contribute_mode_link(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        contrib_target = workspace / "my-fork" / "skills" / "code"
        contrib_target.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(contrib_target)
        target = tmp_path / "source"
        target.mkdir()
        created, fixed = _ensure_skill_link(target, link, workspace)
        assert created == 0
        assert fixed == 0
        assert link.resolve() == contrib_target.resolve()

    def test_leaves_real_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "source"
        target.mkdir()
        link = tmp_path / "link"
        link.mkdir()
        created, fixed = _ensure_skill_link(target, link, tmp_path / "workspace")
        assert created == 0
        assert fixed == 0
        assert link.is_dir()
        assert not link.is_symlink()

    def test_handles_oserror_on_resolve(self, tmp_path: Path) -> None:
        """When resolve() raises OSError, the link is treated as non-contribute and gets fixed."""
        target = tmp_path / "source"
        target.mkdir()
        wrong = tmp_path / "wrong"
        wrong.mkdir()
        link = tmp_path / "link"
        link.symlink_to(wrong)

        original_resolve = Path.resolve

        call_count = 0

        def patched_resolve(self: Path, *args: object, **kwargs: object) -> Path:
            nonlocal call_count
            # First resolve call is inside the try block — raise OSError
            if self == link:
                call_count += 1
                if call_count == 1:
                    msg = "simulated"
                    raise OSError(msg)
            return original_resolve(self, *args, **kwargs)

        with patch.object(Path, "resolve", patched_resolve):
            created, fixed = _ensure_skill_link(target, link, tmp_path / "workspace")
        assert created == 0
        assert fixed == 1


class TestSyncSkillSymlinks:
    def test_creates_symlinks_for_core_skills(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()
        (skills_src / "test").mkdir()
        (skills_src / "test" / "SKILL.md").touch()
        (skills_src / "no-skill").mkdir()  # No SKILL.md — should be skipped

        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            created, fixed = SkillLinker(claude_skills, tmp_path / "workspace").sync()

        assert created == 2
        assert fixed == 0
        assert (claude_skills / "code").is_symlink()
        assert (claude_skills / "test").is_symlink()
        assert not (claude_skills / "no-skill").exists()

    def test_includes_overlay_skills(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()

        overlay_skill = tmp_path / "overlay" / "my-skill"
        overlay_skill.mkdir(parents=True)

        claude_skills = tmp_path / "claude_skills"
        claude_skills.mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = [(overlay_skill, "my-skill")]
            created, _fixed = SkillLinker(claude_skills, tmp_path / "workspace").sync()

        assert created == 1
        assert (claude_skills / "my-skill").is_symlink()

    def test_sync_core_false_skips_core_symlinks(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()

        runtime_skills = tmp_path / "runtime_skills"
        runtime_skills.mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            created, fixed = SkillLinker(runtime_skills, tmp_path / "workspace").sync(sync_core=False)

        assert created == 0
        assert fixed == 0
        assert not (runtime_skills / "code").exists()

    def test_sync_core_false_prunes_existing_core_links(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()

        runtime_skills = tmp_path / "runtime_skills"
        runtime_skills.mkdir()
        (runtime_skills / "code").symlink_to(skills_src / "code")

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            SkillLinker(runtime_skills, tmp_path / "workspace").sync(sync_core=False)

        assert not (runtime_skills / "code").exists()

    def test_sync_core_false_still_adds_overlays(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()

        overlay_skill = tmp_path / "overlay" / "my-skill"
        overlay_skill.mkdir(parents=True)

        runtime_skills = tmp_path / "runtime_skills"
        runtime_skills.mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = [(overlay_skill, "my-skill")]
            created, _fixed = SkillLinker(runtime_skills, tmp_path / "workspace").sync(sync_core=False)

        assert created == 1
        assert (runtime_skills / "my-skill").is_symlink()
        assert not (runtime_skills / "code").exists()


class TestAgentSkillDirs:
    def test_includes_claude_and_codex(self) -> None:
        from teatree.cli.setup import AGENT_SKILL_RUNTIMES  # noqa: PLC0415

        assert "claude" in AGENT_SKILL_RUNTIMES
        assert "codex" in AGENT_SKILL_RUNTIMES

    def test_factory_resolves_against_home(self, tmp_path, monkeypatch) -> None:
        from teatree.cli.setup import agent_skill_dirs  # noqa: PLC0415

        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
        dirs = dict(agent_skill_dirs())
        assert dirs["codex"] == tmp_path / ".codex" / "skills"
        assert dirs["claude"] == tmp_path / ".claude" / "skills"


class TestSetupSyncsCodexWhenDirExists:
    def test_syncs_codex_core_but_leaves_claude_to_plugin(self, tmp_path: Path, monkeypatch) -> None:
        """Claude core skills come from the t3 plugin; Codex gets symlinks.

        Setup should symlink core skills into ~/.codex/skills but NOT into
        ~/.claude/skills.
        """
        from teatree.cli.setup import command as setup_module  # noqa: PLC0415

        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()

        home = tmp_path / "home"
        claude_skills = home / ".claude" / "skills"
        codex_skills = home / ".codex" / "skills"
        claude_skills.mkdir(parents=True)
        codex_skills.mkdir(parents=True)

        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / "apm.yml").touch()
        (repo / ".git").mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch.object(setup_module, "find_main_clone", return_value=repo),
            patch.object(setup_module, "ApmInstaller"),
            patch.object(setup_module, "PluginRegistrar"),
            patch.object(setup_module, "ensure_self_db_migrated", return_value=False),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
            patch("teatree.config.load_config") as mock_load,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            mock_load.return_value.user.contribute = False
            mock_load.return_value.user.excluded_skills = []
            mock_load.return_value.user.workspace_dir = str(tmp_path / "workspace")
            setup_module.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)

        assert not (claude_skills / "code").exists()
        assert (codex_skills / "code").is_symlink()

    def test_prunes_stale_claude_core_symlinks(self, tmp_path: Path, monkeypatch) -> None:
        """Leftover core symlinks from pre-plugin installs are removed.

        ~/.claude/skills/ may still contain symlinks created by earlier
        teatree versions; they must be pruned so they don't shadow the
        plugin's copies of the same skills.
        """
        from teatree.cli.setup import command as setup_module  # noqa: PLC0415

        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()

        home = tmp_path / "home"
        claude_skills = home / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        (claude_skills / "code").symlink_to(skills_src / "code")

        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / "apm.yml").touch()
        (repo / ".git").mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch.object(setup_module, "find_main_clone", return_value=repo),
            patch.object(setup_module, "ApmInstaller"),
            patch.object(setup_module, "PluginRegistrar"),
            patch.object(setup_module, "ensure_self_db_migrated", return_value=False),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
            patch("teatree.config.load_config") as mock_load,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            mock_load.return_value.user.contribute = False
            mock_load.return_value.user.excluded_skills = []
            mock_load.return_value.user.workspace_dir = str(tmp_path / "workspace")
            setup_module.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)

        assert not (claude_skills / "code").exists()

    def test_skips_codex_when_dir_missing(self, tmp_path: Path, monkeypatch) -> None:
        """Setup does not create ~/.codex/skills if it doesn't already exist."""
        from teatree.cli.setup import command as setup_module  # noqa: PLC0415

        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()

        home = tmp_path / "home"
        (home / ".claude" / "skills").mkdir(parents=True)
        # No ~/.codex dir

        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / "apm.yml").touch()
        (repo / ".git").mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch.object(setup_module, "find_main_clone", return_value=repo),
            patch.object(setup_module, "ApmInstaller"),
            patch.object(setup_module, "PluginRegistrar"),
            patch.object(setup_module, "ensure_self_db_migrated", return_value=False),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
            patch("teatree.config.load_config") as mock_load,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            mock_load.return_value.user.contribute = False
            mock_load.return_value.user.excluded_skills = []
            mock_load.return_value.user.workspace_dir = str(tmp_path / "workspace")
            setup_module.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)

        assert not (home / ".codex").exists()


class TestPrunesStaleSkillLinks:
    @staticmethod
    def _make_core_skill(skills_src: Path, name: str) -> Path:
        skill = skills_src / name
        skill.mkdir()
        (skill / "SKILL.md").touch()
        return skill

    def test_prunes_link_for_removed_core_skill(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        self._make_core_skill(skills_src, "wip")
        runtime = tmp_path / "runtime_skills"
        runtime.mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            SkillLinker(runtime, tmp_path / "workspace").sync()
            removed = self._make_core_skill(skills_src, "full-wip")
            SkillLinker(runtime, tmp_path / "workspace").sync()
            shutil.rmtree(removed)
            SkillLinker(runtime, tmp_path / "workspace").sync()

        assert {p.name for p in runtime.iterdir()} == {"wip"}

    def test_prunes_link_for_renamed_core_skill(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        self._make_core_skill(skills_src, "full-wip")
        runtime = tmp_path / "runtime_skills"
        runtime.mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            SkillLinker(runtime, tmp_path / "workspace").sync()
            (skills_src / "full-wip").rename(skills_src / "wip")
            SkillLinker(runtime, tmp_path / "workspace").sync()

        assert {p.name for p in runtime.iterdir()} == {"wip"}

    def test_prunes_link_for_overlay_skill_removed_from_source(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        overlay_root = tmp_path / "overlay" / "skills"
        overlay_skill = overlay_root / "my-skill"
        overlay_skill.mkdir(parents=True)
        (overlay_skill / "SKILL.md").touch()
        runtime = tmp_path / "runtime_skills"
        runtime.mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = [(overlay_skill, "my-skill")]
            SkillLinker(runtime, tmp_path / "workspace").sync()
            assert (runtime / "my-skill").is_symlink()
            shutil.rmtree(overlay_skill)
            mock_svc.collect_overlay_skills.return_value = []
            SkillLinker(runtime, tmp_path / "workspace").sync()

        assert not (runtime / "my-skill").exists()

    def test_leaves_user_owned_real_directory(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        self._make_core_skill(skills_src, "wip")
        runtime = tmp_path / "runtime_skills"
        runtime.mkdir()
        own = runtime / "my-own-skill"
        own.mkdir()
        (own / "SKILL.md").touch()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            SkillLinker(runtime, tmp_path / "workspace").sync()

        assert (runtime / "my-own-skill").is_dir()
        assert not (runtime / "my-own-skill").is_symlink()

    def test_preserves_contribute_mode_link(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        self._make_core_skill(skills_src, "wip")
        workspace = tmp_path / "workspace"
        contrib_target = workspace / "my-fork" / "skills" / "code"
        contrib_target.mkdir(parents=True)
        (contrib_target / "SKILL.md").touch()
        runtime = tmp_path / "runtime_skills"
        runtime.mkdir()
        (runtime / "code").symlink_to(contrib_target)

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.skill_linker.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            SkillLinker(runtime, workspace).sync()

        assert (runtime / "code").resolve() == contrib_target.resolve()


class TestCleanBrokenSymlinks:
    def test_removes_broken(self, tmp_path: Path) -> None:
        (tmp_path / "broken").symlink_to(tmp_path / "nonexistent")
        assert SkillLinker(tmp_path, tmp_path).clean_broken() == 1

    def test_leaves_valid(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        (tmp_path / "valid").symlink_to(target)
        assert SkillLinker(tmp_path, tmp_path).clean_broken() == 0


class TestCoreExcludedSkills:
    def test_default_exclusions_present(self) -> None:
        assert "using-superpowers" in CORE_EXCLUDED_SKILLS
        assert "using-git-worktrees" in CORE_EXCLUDED_SKILLS
