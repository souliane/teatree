# test-path: cross-cutting — drives scripts/hooks/ensure-pr-installed-t3.sh and .pre-commit-config.yaml (no src mirror).
"""The no-orphan pre-push hook runs the INSTALLED ``t3``, never the checkout's own code.

The checkout's editable install resolves a per-worktree isolated control DB, so the
``PullRequest`` ledger row and the ``PendingPullRequest`` obligation a ``uv run`` hook
wrote were invisible to the canonical DB the merge gates read. The wrapper therefore
calls whatever ``t3`` is installed, and only for a branch push: prek leaves
``PRE_COMMIT_REMOTE_BRANCH`` unset in the manual stage ``t3 tool verify-gates`` drives.
"""

import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from teatree.core.forge_push import SHIP_PUSH_ENV, push_branch
from tests._git_repo import make_git_repo, run_git

_ROOT = Path(__file__).resolve().parents[1]
_WRAPPER = _ROOT / "scripts" / "hooks" / "ensure-pr-installed-t3.sh"
_BASH = shutil.which("bash") or "bash"
_GIT = shutil.which("git") or "git"


def _fake_t3(directory: Path, *, exit_code: int = 0) -> Path:
    """A ``t3`` that records its argv beside itself and exits with *exit_code*."""
    directory.mkdir(parents=True, exist_ok=True)
    calls = directory / "t3.calls"
    script = directory / "t3"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {shlex.quote(str(calls))}\nexit {exit_code}\n')
    script.chmod(0o755)
    return calls


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run([_GIT, "init", "-q", "-b", "main", str(root)], check=True)
    return root.resolve()


def _push_hook(
    checkout: Path,
    installed: Path,
    *,
    remote_ref: str | None,
    extra_path: tuple[Path, ...] = (),
    ship_push: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PRE_COMMIT")}
    if ship_push:
        env[SHIP_PUSH_ENV] = "1"
    env["PATH"] = os.pathsep.join([*map(str, extra_path), str(installed), env["PATH"]])
    if remote_ref is not None:
        env["PRE_COMMIT_REMOTE_BRANCH"] = remote_ref
    return subprocess.run([_BASH, str(_WRAPPER)], cwd=checkout, env=env, capture_output=True, text=True, check=False)


def test_argv_names_the_checkout_toplevel_on_a_branch_push(checkout: Path, tmp_path: Path) -> None:
    calls = _fake_t3(tmp_path / "installed")

    proc = _push_hook(checkout, tmp_path / "installed", remote_ref="refs/heads/feat-x")

    assert proc.returncode == 0, proc.stderr
    assert calls.read_text().split() == ["teatree", "pr", "ensure-pr", "--repo", str(checkout)]


@pytest.mark.parametrize("remote_ref", [None, "", "refs/tags/v1", "refs/teatree/claims/widget-7-0123456789abcdef"])
def test_gate_runs_nothing_unless_a_branch_is_pushed(checkout: Path, tmp_path: Path, remote_ref: str | None) -> None:
    calls = _fake_t3(tmp_path / "installed", exit_code=1)

    proc = _push_hook(checkout, tmp_path / "installed", remote_ref=remote_ref)

    assert proc.returncode == 0, proc.stderr
    assert not calls.exists()


def test_path_ignores_a_t3_inside_the_checkout_even_when_it_leads(checkout: Path, tmp_path: Path) -> None:
    installed_calls = _fake_t3(tmp_path / "installed")
    venv_calls = _fake_t3(checkout / ".venv" / "bin")

    proc = _push_hook(
        checkout, tmp_path / "installed", remote_ref="refs/heads/feat-x", extra_path=(checkout / ".venv" / "bin",)
    )

    assert proc.returncode == 0, proc.stderr
    assert installed_calls.exists()
    assert not venv_calls.exists()


@pytest.mark.parametrize("venue_exit", [69, 75, 126, 127, 137])
def test_exit_of_an_unavailable_venue_warns_and_lets_the_push_through(
    checkout: Path, tmp_path: Path, venue_exit: int
) -> None:
    _fake_t3(tmp_path / "installed", exit_code=venue_exit)

    proc = _push_hook(checkout, tmp_path / "installed", remote_ref="refs/heads/feat-x")

    assert proc.returncode == 0
    assert f"t3 <overlay> pr ensure-pr --repo {checkout} --branch feat-x" in proc.stderr


@pytest.mark.parametrize("failure_exit", [1, 2])
def test_exit_of_a_real_failure_passes_through(checkout: Path, tmp_path: Path, failure_exit: int) -> None:
    _fake_t3(tmp_path / "installed", exit_code=failure_exit)

    proc = _push_hook(checkout, tmp_path / "installed", remote_ref="refs/heads/feat-x")

    assert proc.returncode == failure_exit


def test_no_installed_t3_exits_127_skip_with_the_warning(checkout: Path, tmp_path: Path) -> None:
    bare = tmp_path / "bare-bin"
    bare.mkdir()
    for tool in ("git", "bash", "env"):
        (bare / tool).symlink_to(shutil.which(tool) or tool)
    env = {"PATH": str(bare), "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/feat-x"}

    proc = subprocess.run([_BASH, str(_WRAPPER)], cwd=checkout, env=env, capture_output=True, text=True, check=False)

    assert proc.returncode == 0
    assert "pr ensure-pr --repo" in proc.stderr


def test_the_deploy_readme_names_exactly_the_exits_the_wrapper_skips_on() -> None:
    skipped = re.search(r'case "\$rc" in ([\d |]+)\)', _WRAPPER.read_text(encoding="utf-8"))
    readme = (_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    documented = re.search(r"skips with a warning on\s+(.+?)\s+and\s+fails the push", readme, re.DOTALL)

    assert skipped is not None
    assert documented is not None
    assert set(re.findall(r"\d+", documented.group(1))) == set(re.findall(r"\d+", skipped.group(1)))


def test_a_ship_push_runs_nothing_because_the_ship_opens_the_pr_itself(checkout: Path, tmp_path: Path) -> None:
    calls = _fake_t3(tmp_path / "installed", exit_code=1)

    proc = _push_hook(checkout, tmp_path / "installed", remote_ref="refs/heads/feat-x", ship_push=True)

    assert proc.returncode == 0, proc.stderr
    assert not calls.exists()
    assert f"ensure-pr skipped: {SHIP_PUSH_ENV} is set" in proc.stderr


@pytest.fixture
def held_db_write_lock(tmp_path: Path) -> Iterator[Path]:
    """The canonical control DB while ``pr create --sync`` holds ``BEGIN IMMEDIATE`` across its own push."""
    db = tmp_path / "control.sqlite3"
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    yield db
    holder.close()


def _t3_that_owes_a_pr_in(db: Path, directory: Path) -> None:
    """A ``t3`` whose obligation write needs the DB write lock, as ``ensure-pr`` does at ``_owe_pr``."""
    directory.mkdir(parents=True, exist_ok=True)
    write = "import sqlite3,sys; sqlite3.connect(sys.argv[1], timeout=0.2).execute('BEGIN IMMEDIATE')"
    argv = shlex.join([sys.executable, "-c", write, str(db)])
    script = directory / "t3"
    script.write_text(f"#!/bin/sh\nexec {argv}\n")
    script.chmod(0o755)


@pytest.fixture
def clone_pushing_through_the_hook(tmp_path: Path, held_db_write_lock: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    remote = make_git_repo(tmp_path / "origin.git", bare=True)
    clone = make_git_repo(tmp_path / "clone")
    run_git(clone, "remote", "add", "origin", str(remote))
    run_git(clone, "checkout", "-q", "-b", "feat-x")
    run_git(clone, "commit", "-q", "--allow-empty", "-m", "work")
    hook = clone / ".git" / "hooks" / "pre-push"
    wrapper = shlex.join([_BASH, str(_WRAPPER)])
    hook.write_text(f"#!/bin/sh\nPRE_COMMIT_REMOTE_BRANCH=refs/heads/feat-x exec {wrapper}\n")
    hook.chmod(0o755)
    _t3_that_owes_a_pr_in(held_db_write_lock, tmp_path / "installed")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "installed"), os.environ["PATH"]]))
    return clone


def test_a_ship_push_goes_through_while_it_holds_the_control_db_write_lock(
    clone_pushing_through_the_hook: Path,
) -> None:
    outcome = push_branch(repo=clone_pushing_through_the_hook, ship_opens_pr=True)

    assert outcome.ok, outcome.detail


def test_any_other_push_still_fails_on_a_held_lock_so_the_ship_case_is_not_vacuous(
    clone_pushing_through_the_hook: Path,
) -> None:
    outcome = push_branch(repo=clone_pushing_through_the_hook)

    assert not outcome.ok
    assert "database is locked" in outcome.detail


def test_an_inherited_ship_marker_does_not_exempt_a_plain_push(
    clone_pushing_through_the_hook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SHIP_PUSH_ENV, "1")

    outcome = push_branch(repo=clone_pushing_through_the_hook)

    assert not outcome.ok
    assert "database is locked" in outcome.detail
