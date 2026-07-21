"""Real-git integration tests for ``t3 setup``'s git-hook installation step.

Every test pins the checkout set explicitly (or patches discovery): a test that
let discovery run free would probe — and install into — the developer's own
clones.
"""

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from teatree.cli.setup import command as setup_command
from teatree.cli.setup.git_hooks_installer import GitHooksInstaller
from teatree.cli.setup.statusline_installer import StatuslineInstall
from teatree.core.gates.git_hooks_preflight import REQUIRED_HOOK_NAMES, probe_git_hooks
from tests._git_repo import make_git_repo, run_git

_MINIMAL_PREK_CONFIG = """\
default_install_hook_types: [pre-commit, pre-push]
repos:
  - repo: local
    hooks:
      - id: noop
        name: noop
        language: system
        entry: "true"
        pass_filenames: false
        always_run: true
"""

pytestmark = pytest.mark.skipif(shutil.which("prek") is None, reason="prek is not on PATH")


def make_prek_checkout(path: Path) -> Path:
    checkout = make_git_repo(path)
    (checkout / ".pre-commit-config.yaml").write_text(_MINIMAL_PREK_CONFIG, encoding="utf-8")
    return checkout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_prek_checkout(tmp_path / "repo")


def _install(*checkouts: Path) -> list[str]:
    lines: list[str] = []
    GitHooksInstaller(checkouts[0], checkouts=list(checkouts)).install(echo=lines.append)
    return lines


class TestGitHooksInstaller:
    def test_fresh_checkout_ends_with_both_hooks_installed(self, repo: Path) -> None:
        assert probe_git_hooks(repo).missing == REQUIRED_HOOK_NAMES

        lines = _install(repo)

        assert probe_git_hooks(repo).ok
        for name in REQUIRED_HOOK_NAMES:
            assert (repo / ".git" / "hooks" / name).is_file()
        assert any(line.startswith("OK") and "installed git hooks" in line for line in lines)

    def test_an_unprotected_second_clone_is_installed_into_as_well(self, tmp_path: Path) -> None:
        """The container/host split: one clone protected, the other silently not."""
        container = make_prek_checkout(tmp_path / "container-clone")
        host = make_prek_checkout(tmp_path / "host-checkout")
        _install(container)
        assert probe_git_hooks(host).missing == REQUIRED_HOOK_NAMES

        _install(container, host)

        assert probe_git_hooks(host).ok
        assert probe_git_hooks(container).ok

    def test_rerun_is_a_no_op(self, repo: Path) -> None:
        _install(repo)
        hooks = {name: (repo / ".git" / "hooks" / name).read_bytes() for name in REQUIRED_HOOK_NAMES}

        lines = _install(repo)

        for name, body in hooks.items():
            assert (repo / ".git" / "hooks" / name).read_bytes() == body
        assert any("already installed" in line for line in lines)
        assert not any(line.startswith("WARN") for line in lines)

    def test_deliberate_hooks_path_is_left_untouched(self, repo: Path, tmp_path: Path) -> None:
        elsewhere = tmp_path / "operator-hooks"
        elsewhere.mkdir()
        run_git(repo, "config", "core.hooksPath", str(elsewhere))

        lines = _install(repo)

        assert list(elsewhere.iterdir()) == []
        assert not (repo / ".git" / "hooks" / "pre-push").is_file()
        assert run_git(repo, "config", "--get", "core.hooksPath") == str(elsewhere)
        assert any(str(elsewhere) in line for line in lines)

    def test_checkout_without_a_prek_config_is_skipped(self, tmp_path: Path) -> None:
        plain = make_git_repo(tmp_path / "no-config")

        lines = _install(plain)

        assert any("No prek-managed checkout found" in line for line in lines)
        assert probe_git_hooks(plain).missing == REQUIRED_HOOK_NAMES

    def test_non_repo_warns_and_continues(self, tmp_path: Path) -> None:
        (tmp_path / ".pre-commit-config.yaml").write_text(_MINIMAL_PREK_CONFIG, encoding="utf-8")

        lines = _install(tmp_path)

        assert any(line.startswith("WARN") and "not a git checkout" in line for line in lines)


class TestSetupCommandInstallsHooks:
    """``t3 setup`` itself must leave every discovered checkout with its hooks."""

    def test_run_installs_into_every_discovered_checkout(self, tmp_path: Path, monkeypatch) -> None:
        installed_clone = make_prek_checkout(tmp_path / "container-clone")
        host = make_prek_checkout(tmp_path / "host-checkout")
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
        config = MagicMock()
        config.user.excluded_skills = []

        with (
            patch("teatree.cli.setup.git_hooks_installer.discover_checkouts", return_value=[installed_clone, host]),
            patch.object(setup_command, "find_main_clone", return_value=installed_clone),
            patch.object(setup_command, "validate_repo", return_value=installed_clone),
            patch.object(setup_command, "_repair_dep_drift"),
            patch.object(setup_command, "ToolInstaller"),
            patch.object(setup_command, "ApmInstaller"),
            patch.object(setup_command, "strip_apm_hooks", return_value=0),
            patch.object(setup_command, "install_statusline", return_value=StatuslineInstall.ALREADY_PRESENT),
            patch.object(setup_command, "DockerAliasInstaller"),
            patch.object(setup_command, "agent_skill_dirs", return_value=[]),
            patch.object(setup_command, "ensure_self_db_migrated", return_value=False),
            patch.object(setup_command, "seed_default_loops"),
            patch.object(setup_command, "provision_all_overlay_dm_channels"),
            patch.object(setup_command, "ensure_django"),
            patch("teatree.config.load_config", return_value=config),
            patch("teatree.config.clone_root", return_value=tmp_path / "workspace"),
            patch("teatree.cli.recommended_authorizations.report_missing_authorizations"),
        ):
            setup_command.run(SimpleNamespace(invoked_subcommand=None), skip_plugin=True)

        assert probe_git_hooks(installed_clone).ok
        assert probe_git_hooks(host).ok
