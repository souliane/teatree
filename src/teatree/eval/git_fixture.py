"""Throwaway git-repo fixtures for clean-room eval scenarios.

A scenario whose prompt presupposes a working tree — "your changes are staged",
"squash the local commits before merge" — runs in an empty temp dir by default,
so the agent's first ``git`` command returns nothing and it investigates the
mismatch instead of firing the canonical command. That is a false negative: the
skill is correct, the sandbox just lacks the state the prompt describes.

Declaring ``fixture: git_repo`` provisions a real throwaway repo whose state
matches those prompts — a base commit pushed to an ``origin`` remote (so
``origin/main`` and ``git merge-base`` resolve), a ``feat/example`` branch two
commits ahead of it (a squash target), and one staged, uncommitted change (the
"changes are staged" the commit prompt asserts). The agent inspects, finds the
described state, and runs the command.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from teatree.utils.git_run import run_strict as git

GIT_REPO = "git_repo"
KNOWN_FIXTURES = frozenset({GIT_REPO})

#: A deliberately over-branched dispatch carrying a complexity suppression, so a
#: "fix the real cause, don't suppress" scenario has a CONCRETE file+function to
#: refactor (the agent edits this / runs the linter on it rather than answering
#: in prose because the sandbox held no fixable code). Committed on ``main`` so it
#: is present in the working tree of every ``git_repo`` scenario without changing
#: ``feat/example``'s two-commits-ahead squash contract or the staged-change set.
_MESSY_PY = """\
def classify_status(code):  # noqa: C901, PLR0911
    if code == 100:
        return "continue"
    if code == 200:
        return "ok"
    if code == 201:
        return "created"
    if code == 301:
        return "moved"
    if code == 400:
        return "bad request"
    if code == 401:
        return "unauthorized"
    if code == 404:
        return "not found"
    if code == 500:
        return "server error"
    return "unknown"
"""


def _write(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")


@contextmanager
def provision_git_fixture(kind: str) -> Iterator[Path]:
    if kind not in KNOWN_FIXTURES:
        msg = f"unknown eval fixture: {kind!r} (known: {sorted(KNOWN_FIXTURES)})"
        raise ValueError(msg)
    with TemporaryDirectory(prefix="t3-eval-gitfx-") as tmp:
        root = Path(tmp)
        origin = root / "origin.git"
        repo = root / "repo"
        repo.mkdir()
        git(repo=str(root), args=["init", "--bare", "-b", "main", str(origin)])
        git(repo=str(repo), args=["init", "-b", "main"])
        git(repo=str(repo), args=["config", "user.email", "agent@example.com"])
        git(repo=str(repo), args=["config", "user.name", "Eval Agent"])
        git(repo=str(repo), args=["config", "commit.gpgsign", "false"])
        _write(repo, "README.md", "# fixture\n")
        _write(repo, "messy.py", _MESSY_PY)
        git(repo=str(repo), args=["add", "README.md", "messy.py"])
        git(repo=str(repo), args=["commit", "-m", "chore: base"])
        git(repo=str(repo), args=["remote", "add", "origin", str(origin)])
        git(repo=str(repo), args=["push", "-u", "origin", "main"])
        git(repo=str(repo), args=["checkout", "-b", "feat/example"])
        _write(repo, "feature_a.py", "def a():\n    return 1\n")
        git(repo=str(repo), args=["add", "feature_a.py"])
        git(repo=str(repo), args=["commit", "-m", "feat: part a"])
        _write(repo, "feature_b.py", "def b():\n    return 2\n")
        git(repo=str(repo), args=["add", "feature_b.py"])
        git(repo=str(repo), args=["commit", "-m", "feat: part b"])
        _write(repo, "feature_c.py", "def c():\n    return 3\n")
        git(repo=str(repo), args=["add", "feature_c.py"])
        yield repo
