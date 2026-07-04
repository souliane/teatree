"""Environmental resolution for the main-clone gate (the package-native twin).

Real ``git`` under ``tmp_path``: a managed primary clone (``.git`` *dir*, a
``souliane/teatree`` origin so ``slug_for_cwd`` resolves it managed offline) and a
linked worktree (``.git`` *file*). The deny path is anti-vacuous — the mutation is
refused in the clone and allowed in the worktree, so the test goes RED if the
environmental check stops distinguishing them.
"""

from pathlib import Path

import pytest

from teatree.core.gates import main_clone_env as env
from tests._git_repo import make_git_repo, run_git
from tests.teatree_agents.lane_b._managed_clone import linked_worktree, managed_main_clone


class TestMainCloneGitDenyReason:
    def test_mutation_in_a_managed_main_clone_is_denied(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        reason = env.main_clone_git_deny_reason("git checkout feature", clone)
        assert reason is not None
        assert "MAIN CLONE" in reason

    def test_safe_git_in_a_managed_main_clone_is_allowed(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        for command in ("git checkout main", "git fetch origin", "git status"):
            assert env.main_clone_git_deny_reason(command, clone) is None

    def test_mutation_in_a_linked_worktree_is_allowed(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        assert env.main_clone_git_deny_reason("git checkout feature", wt) is None

    def test_mutation_in_an_unmanaged_clone_is_allowed(self, tmp_path: Path) -> None:
        clone = make_git_repo(tmp_path / "random")
        run_git(clone, "remote", "add", "origin", "git@github.com:randomuser/randomrepo.git")
        assert env.main_clone_git_deny_reason("git checkout feature", clone) is None

    def test_dash_c_into_main_clone_from_worktree_cwd_is_denied(self, tmp_path: Path) -> None:
        # The bypass: cwd is the worktree, but ``-C <main-clone>`` redirects the
        # mutation INTO the managed clone — must block despite the benign cwd.
        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        assert env.main_clone_git_deny_reason(f"git -C {clone} checkout feature", wt) is not None

    def test_unresolvable_dash_c_is_allowed(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        # A ``-C`` value carrying a substitution marker cannot be pinned → allow.
        assert env.main_clone_git_deny_reason('git -C "$(pwd)" checkout feature', clone) is None

    def test_non_git_dir_is_allowed(self, tmp_path: Path) -> None:
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        assert env.main_clone_git_deny_reason("git checkout feature", plain) is None

    def test_no_cwd_is_allowed(self) -> None:
        assert env.main_clone_git_deny_reason("git checkout feature", None) is None


class TestEffectiveCommandDir:
    def test_plain_command_keys_off_the_cwd(self, tmp_path: Path) -> None:
        assert env.effective_command_dir("git status", tmp_path) == tmp_path

    def test_dash_c_redirects_the_target_dir(self, tmp_path: Path) -> None:
        other = tmp_path / "other"
        assert env.effective_command_dir(f"git -C {other} status", tmp_path) == other

    def test_git_dir_value_normalises_to_the_repo_root(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        assert env.effective_command_dir(f"git --git-dir {repo}/.git status", tmp_path) == repo

    def test_unresolvable_target_returns_none(self, tmp_path: Path) -> None:
        assert env.effective_command_dir('git -C "$(pwd)" status', tmp_path) is None

    def test_resolver_error_falls_back_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_command: str, _cwd: object) -> object:
            raise RuntimeError

        monkeypatch.setattr("teatree.hooks._commit_repo_dir.resolve_commit_dir", _boom)
        assert env.effective_command_dir("git status", tmp_path) == tmp_path


class TestIsManagedMainClone:
    def test_managed_primary_clone_is_true(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        assert env.is_managed_main_clone(str(clone)) is True

    def test_linked_worktree_is_false(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        assert env.is_managed_main_clone(str(wt)) is False

    def test_unmanaged_clone_is_false(self, tmp_path: Path) -> None:
        clone = make_git_repo(tmp_path / "random")
        run_git(clone, "remote", "add", "origin", "git@github.com:randomuser/randomrepo.git")
        assert env.is_managed_main_clone(str(clone)) is False

    def test_non_git_dir_is_false(self, tmp_path: Path) -> None:
        assert env.is_managed_main_clone(str(tmp_path)) is False

    def test_bad_path_fails_open_to_false(self) -> None:
        assert env.is_managed_main_clone("\x00bad") is False

    def test_worktree_probe_error_fails_open_to_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        clone = managed_main_clone(tmp_path / "teatree")

        def _boom(_root: Path) -> bool:
            raise OSError

        monkeypatch.setattr("teatree.paths.running_from_worktree", _boom)
        assert env.is_managed_main_clone(str(clone)) is False


class TestRepoRootIsTeatreeManaged:
    def test_overlay_path_match_is_managed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo = tmp_path / "product"
        repo.mkdir()
        base = tmp_path.resolve()
        monkeypatch.setattr(env, "_managed_repo_signals", lambda: ([], [base]))
        assert env.repo_root_is_teatree_managed(str(repo)) is True

    def test_bad_repo_path_is_not_managed(self) -> None:
        assert env.repo_root_is_teatree_managed("\x00bad") is False

    def test_overlay_path_non_match_uses_slug(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # An overlay path that does NOT enclose the repo → the path loop's
        # ValueError branch continues, and the slug signal still resolves it.
        clone = managed_main_clone(tmp_path / "teatree")
        unrelated = (tmp_path / "elsewhere").resolve()
        unrelated.mkdir()
        monkeypatch.setattr(env, "_managed_repo_signals", lambda: (["souliane/teatree"], [unrelated]))
        assert env.repo_root_is_teatree_managed(str(clone)) is True

    def test_slug_resolution_error_is_not_managed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        clone = managed_main_clone(tmp_path / "teatree")

        def _boom(_cwd: Path) -> str:
            raise RuntimeError

        monkeypatch.setattr("teatree.hooks._repo_visibility.slug_for_cwd", _boom)
        assert env.repo_root_is_teatree_managed(str(clone)) is False


class TestManagedRepoSignals:
    def test_reads_overlay_repo_slugs_and_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = {
            "o": {"workspace_repos": ["ACME/App"], "public_repos": ["acme/site"], "path": str(tmp_path)},
            "no-path": {"workspace_repos": ["acme/lib"]},  # no `path` → path branch skipped
            "bad-path": {"path": "\x00bad"},  # unresolvable path → suppressed, not raised
            "not-a-dict": "ignored",
        }
        monkeypatch.setattr(env, "_overlays_registry", lambda: registry)
        slugs, paths = env._managed_repo_signals()
        assert {"souliane/teatree", "acme/app", "acme/site", "acme/lib"} <= set(slugs)
        assert paths == [tmp_path.resolve()]

    def test_non_dict_overlay_entry_is_skipped_for_protected_branches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = {"o": {"protected_branches": ["z"]}, "not-a-dict": "ignored"}
        monkeypatch.setattr(env, "_overlays_registry", lambda: registry)
        assert env._load_protected_branches() == {"main", "master", "z"}


class TestDefaultBranch:
    def test_prefers_origin_head_pointer(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree")
        run_git(clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        assert env._default_branch(clone) == "trunk"

    def test_falls_back_to_current_branch(self, tmp_path: Path) -> None:
        clone = managed_main_clone(tmp_path / "teatree", default_branch="develop")
        assert env._default_branch(clone) == "develop"

    def test_git_query_error_yields_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from teatree.utils.run import TimeoutExpired  # noqa: PLC0415

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise TimeoutExpired(cmd="git", timeout=3)

        monkeypatch.setattr(env, "run_allowed_to_fail", _boom)
        assert env._resolve_repo_root(str(tmp_path)) is None


class TestOverlaysRegistry:
    def test_db_row_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = {"o": {"protected_branches": ["x"]}}
        monkeypatch.setattr("teatree.config.cold_reader.read_setting", lambda _key: row)
        assert env._load_protected_branches() >= {"main", "master", "x"}

    def test_db_error_falls_back_to_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_key: str) -> object:
            raise RuntimeError

        monkeypatch.setattr("teatree.config.cold_reader.read_setting", _boom)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".teatree.toml").write_text('[overlays.o]\nprotected_branches = ["dev"]\n')
        assert "dev" in env._load_protected_branches()

    def test_missing_toml_is_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.cold_reader.read_setting", lambda _key: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert env._load_protected_branches() == {"main", "master"}

    def test_broken_toml_is_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.config.cold_reader.read_setting", lambda _key: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".teatree.toml").write_text("this is not = valid = toml [[[")
        assert env._load_protected_branches() == {"main", "master"}
