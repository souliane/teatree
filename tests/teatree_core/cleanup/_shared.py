"""Shared real-git helpers for the teatree.core cleanup test package.

Lifted verbatim from the former monolithic
``tests/teatree_core/test_cleanup.py`` (souliane/teatree#443). No behavior
change: the same ``GIT_*``-stripped environment and ``git -C`` runner the
real-git integration and #835 recovery tests share, relocated so each focused
test module can import them.
"""

import os
import shutil
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

from teatree.core.worktree import branch_classification

_GIT = shutil.which("git") or "/usr/bin/git"
_RM = shutil.which("rm") or "/bin/rm"


def _clean_env() -> dict[str, str]:
    """Env with all ``GIT_*`` stripped (AGENTS.md § Test-Writing Doctrine, #288).

    The suite can run from the inline pre-commit ``pytest`` hook, where the
    outer ``git commit`` exports ``GIT_DIR``/``GIT_INDEX_FILE``/``GIT_WORK_TREE``.
    Inherited, they hijack the tmp-repo ``git`` calls so a test that passes
    standalone corrupts the real repo under ``git commit``.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run([_GIT, "-C", str(cwd), *args], check=True, capture_output=True, env=_clean_env())


def corrupt_index(wt_dir: Path) -> None:
    """Corrupt the real on-disk index for a worktree so ``git status`` itself fails.

    The unanswerable-probe fixture, with no mocking: a ``git worktree add``
    checkout's ``.git`` is a gitdir POINTER file, and its index lives under the main
    repo's ``.git/worktrees/<name>/index`` rather than ``<wt_dir>/.git/index``, so
    the real git dir is resolved through ``rev-parse`` first.
    """
    result = subprocess.run(
        [_GIT, "-C", str(wt_dir), "rev-parse", "--git-dir"],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = wt_dir / git_dir
    (git_dir / "index").write_bytes(b"not a real git index")


def init_pushed_main(tmp: Path) -> Path:
    """A ``main`` clone with a bare file ``origin`` and one base commit pushed."""
    remote = tmp / "remote.git"
    subprocess.run(
        [_GIT, "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    work = tmp / "work"
    work.mkdir()
    _run_git("init", "-q", "-b", "main", cwd=work)
    _run_git("config", "user.email", "t@t", cwd=work)
    _run_git("config", "user.name", "t", cwd=work)
    _run_git("remote", "add", "origin", str(remote), cwd=work)
    (work / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "initial", cwd=work)
    _run_git("push", "-q", "origin", "main", cwd=work)
    _run_git("fetch", "-q", "origin", cwd=work)
    return work


def rev_parse(work: Path, ref: str) -> str:
    result = subprocess.run(
        [_GIT, "-C", str(work), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    return result.stdout.strip()


def squash_then_base_evolved(tmp: Path) -> tuple[Path, str]:
    """The case that defeats every git-local landed instrument (never-pushed branch).

    ``feature`` was squash-merged with a conflict-resolved patch (the squash's
    content differs from the branch's own, as it routinely does when the MR was
    resolved against a moved base), and the base then edited the same file AGAIN
    — so the branch's blob exists nowhere in the base tree, its patch-id matches
    no upstream commit, and its tip is no ancestor. Only the forge's own merge
    record still knows the work landed. Returns the clone and ``feature``'s tip.
    """
    work = init_pushed_main(tmp)
    _run_git("checkout", "-q", "-b", "feature", "main", cwd=work)
    (work / "x.txt").write_text("the feature work\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "feat: add x", cwd=work)
    _run_git("checkout", "-q", "main", cwd=work)
    (work / "x.txt").write_text("the feature work, as resolved at merge\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "feat: add x (#7)", cwd=work)
    (work / "x.txt").write_text("rewritten later on main\n", encoding="utf-8")
    _run_git("add", "-A", cwd=work)
    _run_git("commit", "-q", "-m", "refactor: rewrite x", cwd=work)
    _run_git("push", "-q", "origin", "main", cwd=work)
    _run_git("fetch", "-q", "origin", cwd=work)
    return work, rev_parse(work, "feature")


def forge_reporting(*, merged_head_sha: str = "", open_pr: bool = False) -> AbstractContextManager[object]:
    """A fake forge behind ``probe_host_cli``: a merged MR at *merged_head_sha*, an open one, or neither.

    The fake serves whatever ``extract`` asks of the payload row, so one stub
    answers the merged-number, merged-head-sha and open-number probes alike. It
    deliberately carries NO merge-commit sha (extract fails → ``""``), so the
    tree-matches-squash layer stays inert and each test exercises only the rung
    it names.
    """

    def fake(cmd: list[str], repo: str, extract: Callable[[object], str], *, timeout: float = 30.0) -> str:
        del repo, timeout
        querying_merged = "merged" in " ".join(cmd)
        if querying_merged and not merged_head_sha:
            return ""
        if not querying_merged and not open_pr:
            return ""
        row = {"number": 7, "iid": 7, "headRefOid": merged_head_sha, "sha": merged_head_sha}
        try:
            return str(extract([row]) or "")
        except (IndexError, KeyError, TypeError):
            return ""

    return patch.object(branch_classification, "probe_host_cli", side_effect=fake)
