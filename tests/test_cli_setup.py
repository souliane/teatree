"""Tests for t3 setup — global skill installation command."""

import json
from pathlib import Path
from unittest.mock import patch

import click
import pytest

from teatree.cli.setup import (
    CORE_EXCLUDED_SKILLS,
    _clean_broken_symlinks,
    _ensure_skill_link,
    _ensure_t3_installed,
    _find_main_clone,
    _install_claude_plugin,
    _register_claude_marketplace,
    _remove_excluded_skills,
    _run_apm_install,
    _strip_apm_hooks,
    _sync_skill_symlinks,
    _validate_repo,
)


class TestFindMainClone:
    def test_returns_none_when_no_repo(self) -> None:
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = None
            assert _find_main_clone() is None

    def test_returns_none_when_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /some/other/path\n")
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = repo
            assert _find_main_clone() is None

    def test_returns_repo_when_main_clone(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = repo
            result = _find_main_clone()
            assert result == repo


class TestRunApmInstall:
    def test_returns_false_when_apm_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            assert _run_apm_install(Path("/fake")) is False

    def test_returns_false_on_failure(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/apm"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "some error"
            assert _run_apm_install(tmp_path) is False

    def test_returns_true_on_success(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/apm"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            assert _run_apm_install(tmp_path) is True
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert args[0][0] == ["/usr/bin/apm", "install", "-g", "--target", "claude"]


class TestRegisterClaudeMarketplace:
    def test_returns_true_on_success(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            assert _register_claude_marketplace("/usr/bin/claude", tmp_path) is True

    def test_treats_already_registered_as_success(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "marketplace already added"
            assert _register_claude_marketplace("/usr/bin/claude", tmp_path) is True

    def test_returns_false_on_other_failure(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "permission denied"
            assert _register_claude_marketplace("/usr/bin/claude", tmp_path) is False


class TestInstallClaudePlugin:
    def test_skips_when_claude_missing(self, tmp_path: Path) -> None:
        with patch("shutil.which", return_value=None):
            assert _install_claude_plugin(tmp_path, scope="user") is False

    def test_returns_false_when_marketplace_fails(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            assert _install_claude_plugin(tmp_path, scope="user") is False

    def test_installs_via_claude_cli(self, tmp_path: Path) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            assert _install_claude_plugin(tmp_path, scope="user") is True
            assert mock_run.call_count == 2  # marketplace add + plugin install

    def test_returns_false_when_plugin_install_fails(self, tmp_path: Path) -> None:
        from subprocess import CompletedProcess  # noqa: PLC0415

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch(
                "subprocess.run",
                side_effect=[
                    CompletedProcess(args=[], returncode=0, stderr=""),
                    CompletedProcess(args=[], returncode=1, stderr="install failed"),
                ],
            ),
        ):
            assert _install_claude_plugin(tmp_path, scope="user") is False


class TestStripApmHooks:
    def test_no_file(self, tmp_path: Path) -> None:
        assert _strip_apm_hooks(tmp_path / "nonexistent.json") == 0

    def test_no_hooks(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"key": "value"}))
        assert _strip_apm_hooks(settings) == 0

    def test_removes_apm_entries(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "UserPromptSubmit": [
                    {"type": "command", "command": "my-hook"},
                    {"type": "command", "command": "apm-hook", "_apm_source": "teatree"},
                ],
            },
        }
        settings.write_text(json.dumps(data))
        removed = _strip_apm_hooks(settings)
        assert removed == 1
        result = json.loads(settings.read_text())
        assert len(result["hooks"]["UserPromptSubmit"]) == 1
        assert result["hooks"]["UserPromptSubmit"][0]["command"] == "my-hook"

    def test_removes_empty_hook_keys(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "UserPromptSubmit": [
                    {"type": "command", "_apm_source": "teatree"},
                ],
            },
        }
        settings.write_text(json.dumps(data))
        removed = _strip_apm_hooks(settings)
        assert removed == 1
        result = json.loads(settings.read_text())
        assert "hooks" not in result

    def test_invalid_json(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("not json")
        assert _strip_apm_hooks(settings) == 0

    def test_hooks_not_a_dict(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": "not-a-dict"}))
        assert _strip_apm_hooks(settings) == 0


class TestRemoveExcludedSkills:
    def test_removes_symlinks(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "using-superpowers"
        link.symlink_to(target)
        assert _remove_excluded_skills(tmp_path, ["using-superpowers"]) == 1
        assert not link.exists()

    def test_removes_directories(self, tmp_path: Path) -> None:
        skill_dir = tmp_path / "using-git-worktrees"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").touch()
        assert _remove_excluded_skills(tmp_path, ["using-git-worktrees"]) == 1
        assert not skill_dir.exists()

    def test_ignores_nonexistent(self, tmp_path: Path) -> None:
        assert _remove_excluded_skills(tmp_path, ["nonexistent"]) == 0

    def test_multiple_excluded(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        (tmp_path / "skill-a").symlink_to(target)
        (tmp_path / "skill-b").mkdir()
        assert _remove_excluded_skills(tmp_path, ["skill-a", "skill-b", "skill-c"]) == 2

    def test_rejects_path_traversal(self, tmp_path: Path) -> None:
        assert _remove_excluded_skills(tmp_path, ["../etc", ".hidden"]) == 0

    def test_rejects_nested_path(self, tmp_path: Path) -> None:
        assert _remove_excluded_skills(tmp_path, ["foo/bar"]) == 0


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
            patch("teatree.cli.setup.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            created, fixed = _sync_skill_symlinks(claude_skills, tmp_path / "workspace")

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
            patch("teatree.cli.setup.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = [(overlay_skill, "my-skill")]
            created, _fixed = _sync_skill_symlinks(claude_skills, tmp_path / "workspace")

        assert created == 1
        assert (claude_skills / "my-skill").is_symlink()


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
    def test_syncs_to_both_claude_and_codex(self, tmp_path: Path, monkeypatch) -> None:
        """Setup mirrors skill symlinks into ~/.codex/skills when it exists."""
        from teatree.cli import setup as setup_module  # noqa: PLC0415

        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()

        # Simulate home layout: both dirs already exist so setup should target both
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
            patch.object(setup_module, "_find_main_clone", return_value=repo),
            patch.object(setup_module, "_run_apm_install", return_value=True),
            patch.object(setup_module, "_install_claude_plugin", return_value=True),
            patch.object(setup_module, "DoctorService") as mock_svc,
            patch("teatree.config.load_config") as mock_load,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            mock_load.return_value.user.contribute = False
            mock_load.return_value.user.excluded_skills = []
            mock_load.return_value.user.workspace_dir = str(tmp_path / "workspace")
            setup_module.run(claude_scope="user", skip_plugin=True)

        assert (claude_skills / "code").is_symlink()
        assert (codex_skills / "code").is_symlink()

    def test_skips_codex_when_dir_missing(self, tmp_path: Path, monkeypatch) -> None:
        """Setup does not create ~/.codex/skills if it doesn't already exist."""
        from teatree.cli import setup as setup_module  # noqa: PLC0415

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
            patch.object(setup_module, "_find_main_clone", return_value=repo),
            patch.object(setup_module, "_run_apm_install", return_value=True),
            patch.object(setup_module, "_install_claude_plugin", return_value=True),
            patch.object(setup_module, "DoctorService") as mock_svc,
            patch("teatree.config.load_config") as mock_load,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            mock_load.return_value.user.contribute = False
            mock_load.return_value.user.excluded_skills = []
            mock_load.return_value.user.workspace_dir = str(tmp_path / "workspace")
            setup_module.run(claude_scope="user", skip_plugin=True)

        assert not (home / ".codex").exists()


class TestCleanBrokenSymlinks:
    def test_removes_broken(self, tmp_path: Path) -> None:
        (tmp_path / "broken").symlink_to(tmp_path / "nonexistent")
        assert _clean_broken_symlinks(tmp_path) == 1

    def test_leaves_valid(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        (tmp_path / "valid").symlink_to(target)
        assert _clean_broken_symlinks(tmp_path) == 0


class TestValidateRepo:
    def test_exits_when_no_repo(self) -> None:
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = None
            with pytest.raises(click.exceptions.Exit):
                _validate_repo(None)

    def test_exits_when_worktree(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /other\n")
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = repo
            with pytest.raises(click.exceptions.Exit):
                _validate_repo(None)

    def test_exits_when_no_apm_yml(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").mkdir()
        with pytest.raises(click.exceptions.Exit):
            _validate_repo(repo)

    def test_returns_repo_when_valid(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "apm.yml").touch()
        assert _validate_repo(repo) == repo


class TestCoreExcludedSkills:
    def test_default_exclusions_present(self) -> None:
        assert "using-superpowers" in CORE_EXCLUDED_SKILLS
        assert "using-git-worktrees" in CORE_EXCLUDED_SKILLS


class TestEnsureT3Installed:
    def test_skips_install_when_t3_on_path(self, tmp_path: Path) -> None:
        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.cli.setup.subprocess.run") as mock_run,
        ):
            mock_which.side_effect = lambda name: "/usr/local/bin/t3" if name == "t3" else None
            assert _ensure_t3_installed(tmp_path) is True
            mock_run.assert_not_called()

    def test_returns_false_when_uv_missing(self, tmp_path: Path) -> None:
        with patch("teatree.cli.setup.shutil.which", return_value=None):
            assert _ensure_t3_installed(tmp_path) is False

    def test_installs_editable_when_t3_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.cli.setup.subprocess.run") as mock_run,
        ):
            mock_which.side_effect = lambda name: "/usr/bin/uv" if name == "uv" else None
            mock_run.return_value.returncode = 0
            mock_run.return_value.stderr = ""
            assert _ensure_t3_installed(repo) is True
            args = mock_run.call_args[0][0]
            assert args[:3] == ["/usr/bin/uv", "tool", "install"]
            assert "--editable" in args
            assert str(repo) in args

    def test_returns_false_on_install_failure(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.cli.setup.subprocess.run") as mock_run,
        ):
            mock_which.side_effect = lambda name: "/usr/bin/uv" if name == "uv" else None
            mock_run.return_value.returncode = 1
            mock_run.return_value.stderr = "boom"
            assert _ensure_t3_installed(repo) is False
