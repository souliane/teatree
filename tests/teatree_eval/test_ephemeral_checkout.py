"""Throwaway isolated checkout for sub-agent-spawning eval scenarios.

The metered ``sdk`` lane's spawning scenarios make the SDK sub-agent do real git
work; without isolation the sub-agent locates the developer's REAL clone (via the
editable install and the shared ``.git``) and corrupts it. These tests exercise
the isolation MECHANISM against a REAL git repo under ``tmp_path`` — no live SDK
sub-agent is spawned: the contract is "the throwaway is created, the resolution
levers point at it, and it is cleaned up", proven without metering a model.
"""

import os
import sys
from pathlib import Path

import pytest

from teatree.eval.ephemeral_checkout import (
    EphemeralCheckoutError,
    ephemeral_checkout_env,
    provision_ephemeral_checkout,
    resolve_teatree_repo_root,
)
from teatree.utils.run import run_checked
from tests._git_repo import make_git_repo, run_git


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return run_checked(["git", "-C", str(repo), *args], env=env).stdout.strip()


def _init_repo(root: Path) -> None:
    """A minimal real git repo with one commit, mirroring the teatree src layout."""
    make_git_repo(root, initial_commit=False)
    (root / "src" / "teatree").mkdir(parents=True, exist_ok=True)
    (root / "src" / "teatree" / "__init__.py").write_text("VERSION = '0'\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-qm", "init")


class TestResolveTeatreeRepoRoot:
    def test_resolves_the_running_teatree_clone_to_a_git_toplevel(self) -> None:
        root = resolve_teatree_repo_root()
        assert root is not None
        assert (root / "src" / "teatree" / "__init__.py").is_file()
        assert Path(_git(root, "rev-parse", "--show-toplevel")) == root

    def test_returns_none_when_not_a_git_checkout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `git rev-parse --show-toplevel` outside a repo exits non-zero; the lenient
        # runner yields an empty string, which the resolver maps to None.
        monkeypatch.setattr("teatree.eval.ephemeral_checkout.git_run", lambda **_kwargs: "")
        assert resolve_teatree_repo_root() is None


class TestProvisionEphemeralCheckout:
    def test_creates_a_detached_worktree_that_is_not_the_real_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = tmp_path / "real-clone"
        _init_repo(real)
        monkeypatch.setattr("teatree.eval.ephemeral_checkout.resolve_teatree_repo_root", lambda: real)

        with provision_ephemeral_checkout() as checkout:
            assert checkout.is_dir()
            assert checkout != real
            assert (checkout / "src" / "teatree" / "__init__.py").is_file()
            # A detached HEAD worktree of the real clone — sub-agent writes land here.
            assert _git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", (
                "ephemeral checkout must be detached, not on a real branch"
            )

    def test_sub_agent_writes_land_in_the_ephemeral_not_the_real_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = tmp_path / "real-clone"
        _init_repo(real)
        monkeypatch.setattr("teatree.eval.ephemeral_checkout.resolve_teatree_repo_root", lambda: real)

        real_status_before = _git(real, "status", "--porcelain")
        real_branch_before = _git(real, "rev-parse", "--abbrev-ref", "HEAD")

        with provision_ephemeral_checkout() as checkout:
            # Simulate the destructive sub-agent: create a file, switch branch, commit.
            (checkout / "src" / "teatree" / "core").mkdir(parents=True, exist_ok=True)
            (checkout / "src" / "teatree" / "core" / "session.py").write_text("x = 1\n", encoding="utf-8")
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "sub",
                "GIT_AUTHOR_EMAIL": "sub@example.com",
                "GIT_COMMITTER_NAME": "sub",
                "GIT_COMMITTER_EMAIL": "sub@example.com",
            }
            _git(checkout, "checkout", "-q", "-b", "phantom-branch")
            _git(checkout, "add", "-A")
            _git(checkout, "commit", "-qm", "phantom session.py", env=env)

        # The real clone's working tree and branch are UNTOUCHED.
        assert _git(real, "status", "--porcelain") == real_status_before, (
            "the sub-agent's writes leaked into the real clone"
        )
        assert _git(real, "rev-parse", "--abbrev-ref", "HEAD") == real_branch_before, (
            "the sub-agent switched the real clone's branch"
        )
        assert not (real / "src" / "teatree" / "core" / "session.py").exists()

    def test_cleans_up_the_checkout_on_exit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real = tmp_path / "real-clone"
        _init_repo(real)
        monkeypatch.setattr("teatree.eval.ephemeral_checkout.resolve_teatree_repo_root", lambda: real)

        with provision_ephemeral_checkout() as checkout:
            captured = checkout
            assert captured.is_dir()
        assert not captured.exists(), "the ephemeral checkout directory must be removed on exit"
        registered = _git(real, "worktree", "list", "--porcelain")
        assert str(captured) not in registered, "the ephemeral worktree must be deregistered from the real clone"

    def test_cleans_up_even_when_the_body_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        real = tmp_path / "real-clone"
        _init_repo(real)
        monkeypatch.setattr("teatree.eval.ephemeral_checkout.resolve_teatree_repo_root", lambda: real)

        boom = RuntimeError("boom")

        def _provision_then_raise() -> Path:
            with provision_ephemeral_checkout() as checkout:
                raised_in[0] = checkout
                raise boom

        raised_in: list[Path | None] = [None]
        with pytest.raises(RuntimeError, match="boom"):
            _provision_then_raise()
        assert raised_in[0] is not None
        assert not raised_in[0].exists()

    def test_refuses_when_real_root_unresolvable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.eval.ephemeral_checkout.resolve_teatree_repo_root", lambda: None)
        with pytest.raises(EphemeralCheckoutError, match="REFUSES to run"), provision_ephemeral_checkout():
            pass


class TestEphemeralCheckoutEnv:
    def test_prepends_ephemeral_src_to_pythonpath(self) -> None:
        checkout = Path("/tmp/ephem/teatree")
        env = ephemeral_checkout_env({"PYTHONPATH": "/real/src"}, checkout)
        first_entry = env["PYTHONPATH"].split(os.pathsep)[0]
        assert first_entry == str(checkout / "src")
        assert "/real/src" in env["PYTHONPATH"]

    def test_sets_pythonpath_when_absent(self) -> None:
        checkout = Path("/tmp/ephem/teatree")
        env = ephemeral_checkout_env({}, checkout)
        assert env["PYTHONPATH"] == str(checkout / "src")

    def test_clears_inherited_git_pins(self) -> None:
        checkout = Path("/tmp/ephem/teatree")
        env = ephemeral_checkout_env(
            {"GIT_DIR": "/real/.git", "GIT_WORK_TREE": "/real", "GIT_CEILING_DIRECTORIES": "/real"},
            checkout,
        )
        assert "GIT_DIR" not in env
        assert "GIT_WORK_TREE" not in env
        assert "GIT_CEILING_DIRECTORIES" not in env

    def test_does_not_mutate_the_base_env(self) -> None:
        checkout = Path("/tmp/ephem/teatree")
        base = {"PYTHONPATH": "/real/src", "GIT_DIR": "/real/.git"}
        ephemeral_checkout_env(base, checkout)
        assert base == {"PYTHONPATH": "/real/src", "GIT_DIR": "/real/.git"}

    def test_import_teatree_resolves_into_the_ephemeral_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The editable-install foot-gun: prove the env overlay makes `import teatree`
        # resolve into the throwaway, not the real clone's src.
        real = tmp_path / "real-clone"
        _init_repo(real)
        monkeypatch.setattr("teatree.eval.ephemeral_checkout.resolve_teatree_repo_root", lambda: real)
        with provision_ephemeral_checkout() as checkout:
            env = ephemeral_checkout_env(dict(os.environ), checkout)
            env.pop("PYTHONSTARTUP", None)
            resolved = run_checked(
                [sys.executable, "-c", "import teatree; print(teatree.__file__)"],
                env=env,
                cwd=str(checkout),
            ).stdout.strip()
            assert Path(resolved) == checkout / "src" / "teatree" / "__init__.py"
            assert str(real) not in resolved
