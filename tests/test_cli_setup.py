"""Tests for t3 setup — global skill installation command."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
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

GIT_BIN = shutil.which("git") or "git"


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with an empty commit so `git worktree add` works."""
    import subprocess  # noqa: PLC0415

    path.mkdir(parents=True, exist_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run([GIT_BIN, "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run([GIT_BIN, "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True, env=env)


class TestFindMainClone:
    def test_returns_none_when_no_repo(self) -> None:
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = None
            assert _find_main_clone() is None

    def test_resolves_worktree_to_main_clone(self, tmp_path: Path) -> None:
        import subprocess  # noqa: PLC0415

        main_clone = tmp_path / "teatree"
        _init_git_repo(main_clone)
        worktree = tmp_path / "wt"
        subprocess.run(
            [GIT_BIN, "worktree", "add", "-q", "-b", "feature", str(worktree)],
            cwd=main_clone,
            check=True,
        )
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = worktree
            assert _find_main_clone() == main_clone

    def test_returns_repo_when_main_clone(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").mkdir()
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = repo
            result = _find_main_clone()
            assert result == repo

    def test_returns_none_when_git_file_unparseable(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        (repo / ".git").write_text("not a gitdir line\n")
        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = repo
            assert _find_main_clone() is None

    def test_env_var_wins_over_cwd_heuristic(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``T3_REPO`` env var must take priority so setup from a worktree still targets the configured main clone."""
        main_clone = tmp_path / "main-clone"
        main_clone.mkdir()
        (main_clone / ".git").mkdir()
        (main_clone / "pyproject.toml").touch()
        monkeypatch.setenv("T3_REPO", str(main_clone))

        with patch("teatree.cli.setup.DoctorService") as mock_svc:
            mock_svc.find_teatree_repo.return_value = tmp_path / "some-worktree"
            assert _find_main_clone() == main_clone
            mock_svc.find_teatree_repo.assert_not_called()


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

    def test_sync_core_false_skips_core_symlinks(self, tmp_path: Path) -> None:
        skills_src = tmp_path / "core_skills"
        skills_src.mkdir()
        (skills_src / "code").mkdir()
        (skills_src / "code" / "SKILL.md").touch()

        runtime_skills = tmp_path / "runtime_skills"
        runtime_skills.mkdir()

        with (
            patch("teatree.agents.skill_bundle.DEFAULT_SKILLS_DIR", skills_src),
            patch("teatree.cli.setup.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            created, fixed = _sync_skill_symlinks(runtime_skills, tmp_path / "workspace", sync_core=False)

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
            patch("teatree.cli.setup.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = []
            _sync_skill_symlinks(runtime_skills, tmp_path / "workspace", sync_core=False)

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
            patch("teatree.cli.setup.DoctorService") as mock_svc,
        ):
            mock_svc.collect_overlay_skills.return_value = [(overlay_skill, "my-skill")]
            created, _fixed = _sync_skill_symlinks(runtime_skills, tmp_path / "workspace", sync_core=False)

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
        from teatree.cli import setup as setup_module  # noqa: PLC0415

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
            setup_module.run(SimpleNamespace(invoked_subcommand=None), claude_scope="user", skip_plugin=True)

        assert not (claude_skills / "code").exists()
        assert (codex_skills / "code").is_symlink()

    def test_prunes_stale_claude_core_symlinks(self, tmp_path: Path, monkeypatch) -> None:
        """Leftover core symlinks from pre-plugin installs are removed.

        ~/.claude/skills/ may still contain symlinks created by earlier
        teatree versions; they must be pruned so they don't shadow the
        plugin's copies of the same skills.
        """
        from teatree.cli import setup as setup_module  # noqa: PLC0415

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
            setup_module.run(SimpleNamespace(invoked_subcommand=None), claude_scope="user", skip_plugin=True)

        assert not (claude_skills / "code").exists()

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
            setup_module.run(SimpleNamespace(invoked_subcommand=None), claude_scope="user", skip_plugin=True)

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


def _install_run_side_effect(
    uv_tools_dir: Path,
    *,
    install_returncode: int = 0,
    install_stderr: str = "",
) -> Callable[..., SimpleNamespace]:
    """Build a ``subprocess.run`` side effect covering ``uv tool dir`` + install."""

    def side_effect(cmd: list[str], *args: object, **kwargs: object) -> SimpleNamespace:
        if cmd[:3] == ["/usr/bin/uv", "tool", "dir"]:
            stdout = "" if "--bin" in cmd else f"{uv_tools_dir}\n"
            return SimpleNamespace(returncode=0, stderr="", stdout=stdout)
        if cmd[:3] == ["/usr/bin/uv", "tool", "install"]:
            return SimpleNamespace(returncode=install_returncode, stderr=install_stderr, stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    return side_effect


def _which_t3_and_uv(name: str) -> str | None:
    return {"t3": "/usr/local/bin/t3", "uv": "/usr/bin/uv"}.get(name)


class TestEnsureT3Installed:
    def test_skips_when_editable_source_exists(self, tmp_path: Path) -> None:
        uv_tools_dir = tmp_path / "uv-tools"
        teatree_tool = uv_tools_dir / "teatree"
        teatree_tool.mkdir(parents=True)
        editable_source = tmp_path / "main-clone"
        editable_source.mkdir()
        (teatree_tool / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "teatree", editable = "{editable_source}" }}]\n'
        )

        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=_install_run_side_effect(uv_tools_dir)) as mock_run,
        ):
            mock_which.side_effect = _which_t3_and_uv
            assert _ensure_t3_installed(editable_source) is True
            # Only the `uv tool dir` receipt lookup — no install invoked.
            install_calls = [c for c in mock_run.call_args_list if c[0][0][:3] == ["/usr/bin/uv", "tool", "install"]]
            assert install_calls == []

    def test_skips_when_install_is_non_editable(self, tmp_path: Path) -> None:
        uv_tools_dir = tmp_path / "uv-tools"
        teatree_tool = uv_tools_dir / "teatree"
        teatree_tool.mkdir(parents=True)
        (teatree_tool / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "teatree" }]\n')

        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=_install_run_side_effect(uv_tools_dir)) as mock_run,
        ):
            mock_which.side_effect = _which_t3_and_uv
            assert _ensure_t3_installed(tmp_path / "main-clone") is True
            install_calls = [c for c in mock_run.call_args_list if c[0][0][:3] == ["/usr/bin/uv", "tool", "install"]]
            assert install_calls == []

    def test_reinstalls_when_editable_source_missing(self, tmp_path: Path) -> None:
        """Stale editable install (worktree deleted) must be repaired from the main clone."""
        uv_tools_dir = tmp_path / "uv-tools"
        teatree_tool = uv_tools_dir / "teatree"
        teatree_tool.mkdir(parents=True)
        deleted_worktree = tmp_path / "deleted-worktree"
        (teatree_tool / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "teatree", editable = "{deleted_worktree}" }}]\n'
        )
        main_clone = tmp_path / "main-clone"
        main_clone.mkdir()

        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=_install_run_side_effect(uv_tools_dir)) as mock_run,
        ):
            mock_which.side_effect = _which_t3_and_uv
            assert _ensure_t3_installed(main_clone) is True
            install_calls = [c for c in mock_run.call_args_list if c[0][0][:3] == ["/usr/bin/uv", "tool", "install"]]
            assert len(install_calls) == 1
            args = install_calls[0][0][0]
            assert "--force" in args
            assert "--editable" in args
            assert str(main_clone) in args

    def test_returns_false_when_uv_missing(self, tmp_path: Path) -> None:
        with patch("teatree.cli.setup.shutil.which", return_value=None):
            assert _ensure_t3_installed(tmp_path) is False

    def test_returns_true_when_t3_on_path_without_uv(self, tmp_path: Path) -> None:
        """Pipx or other non-uv installs are respected — we only touch uv tool installs."""
        with patch("teatree.cli.setup.shutil.which") as mock_which:
            mock_which.side_effect = lambda name: "/usr/local/bin/t3" if name == "t3" else None
            assert _ensure_t3_installed(tmp_path) is True

    def test_installs_editable_when_t3_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        uv_tools_dir = tmp_path / "uv-tools"
        uv_tools_dir.mkdir()
        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=_install_run_side_effect(uv_tools_dir)) as mock_run,
        ):
            mock_which.side_effect = lambda name: "/usr/bin/uv" if name == "uv" else None
            assert _ensure_t3_installed(repo) is True
            install_calls = [c for c in mock_run.call_args_list if c[0][0][:3] == ["/usr/bin/uv", "tool", "install"]]
            assert len(install_calls) == 1
            args = install_calls[0][0][0]
            assert "--force" in args
            assert "--editable" in args
            assert str(repo) in args

    def test_returns_false_on_install_failure(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        uv_tools_dir = tmp_path / "uv-tools"
        uv_tools_dir.mkdir()
        side_effect = _install_run_side_effect(uv_tools_dir, install_returncode=1, install_stderr="boom")
        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=side_effect),
        ):
            mock_which.side_effect = lambda name: "/usr/bin/uv" if name == "uv" else None
            assert _ensure_t3_installed(repo) is False

    def test_prints_shell_rc_hint_when_still_not_on_path(
        self,
        tmp_path: Path,
        capsys: "pytest.CaptureFixture[str]",
    ) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        bin_dir = tmp_path / "uv-bin"
        bin_dir.mkdir()

        def mock_run_side_effect(cmd: list[str], *args: object, **kwargs: object) -> SimpleNamespace:
            stdout = f"{bin_dir}\n" if cmd[:3] == ["/usr/bin/uv", "tool", "dir"] else ""
            return SimpleNamespace(returncode=0, stderr="", stdout=stdout)

        with (
            patch("teatree.cli.setup.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=mock_run_side_effect),
        ):
            mock_which.side_effect = lambda name: "/usr/bin/uv" if name == "uv" else None
            _ensure_t3_installed(repo)

        out = capsys.readouterr().out
        assert str(bin_dir) in out
        assert "is not on your PATH" in out
        assert 'export PATH="' in out
