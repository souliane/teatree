"""What the session-end work-check tests share: an isolated hook state dir, no real probe, a real checkout.

Both ``test_session_end_work_check`` (what an end leaves for the next session) and
``test_session_end_work_probes`` (the probe behind each state) run every test inside
:func:`session_end_sandbox`.
"""

import contextlib
import shutil
import subprocess
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.session_end_work_check as work_check
from hooks.scripts import stranded_work_report
from hooks.scripts.hook_router import handle_session_end

PROJECT = "/work/project"
GIT = shutil.which("git") or "git"


@contextlib.contextmanager
def session_end_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An isolated hook state dir, and never a shell-out to the real t3 / gh from a unit test."""
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_HOOK_STATE_DIR", str(router.STATE_DIR))
    monkeypatch.setenv("TEATREE_CLAUDE_STATUSLINE_STATE_DIR", str(router.STATE_DIR))
    try:
        with (
            patch.object(work_check, "fetch_orphans", return_value=[]),
            patch.object(work_check, "read_open_prs", return_value=[]),
        ):
            yield
    finally:
        router.STATE_DIR = original


def run_end(data: dict) -> tuple[bool | None, str, str]:
    stdout, stderr = StringIO(), StringIO()
    with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
        verdict = handle_session_end(data)
    return verdict, stdout.getvalue(), stderr.getvalue()


def context(data: dict) -> str:
    """What the session leaves for the next one in its checkout — printed nowhere, decided on nothing."""
    data = {"cwd": PROJECT, **data}
    assert run_end(data) == (None, "", "")
    return stranded_work_report.claim(data["cwd"]).text


def git(repo: Path, *args: str) -> None:
    env = {"HOME": str(repo.parent), "PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null"}
    subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True, env=env)


def repo_with_commit(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "work-branch")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "initial")
    return repo
