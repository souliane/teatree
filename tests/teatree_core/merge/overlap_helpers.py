"""Real-git repositories and a recording fake forge for the open-head overlap tests."""

import shutil
import subprocess
from pathlib import Path

NUMBERS = {"a": 11, "b": 12, "c": 13}
_GIT = shutil.which("git") or "/usr/bin/git"


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [_GIT, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(repo)},
    )
    return done.stdout.strip()


def commit(repo: Path, message: str) -> None:
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message)


def commit_on_branch(repo: Path, branch: str, filename: str, content: str) -> str:
    git(repo, "checkout", "-q", "-b", branch, "main")
    (repo / filename).write_text(content, encoding="utf-8")
    git(repo, "add", filename)
    commit(repo, branch)
    return git(repo, "rev-parse", "HEAD")


def make_three_heads(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A clone with heads a and b editing the same line of one file and head c editing another file."""
    repo = tmp_path / "clone"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "shared.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (repo / "other.txt").write_text("alpha\n", encoding="utf-8")
    git(repo, "add", ".")
    commit(repo, "base")
    shas = {
        "a": commit_on_branch(repo, "pr-a", "shared.txt", "one\nAAA\nthree\n"),
        "b": commit_on_branch(repo, "pr-b", "shared.txt", "one\nBBB\nthree\n"),
        "c": commit_on_branch(repo, "pr-c", "other.txt", "gamma\n"),
    }
    return repo, shas


class RecordingForge:
    def __init__(self, prs: list[dict[str, object]], *, unreadable: bool = False) -> None:
        self.prs = prs
        self.unreadable = unreadable
        self.list_calls: list[tuple[str, str]] = []

    def list_prs(self, *, repo: str, state: str = "", author: str = "") -> list[dict[str, object]]:
        self.list_calls.append((repo, state))
        if self.unreadable:
            msg = "forge down"
            raise RuntimeError(msg)
        return self.prs

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        return ""


def forge_for(shas: dict[str, str]) -> RecordingForge:
    return RecordingForge([{"number": NUMBERS[key], "head": {"sha": sha}, "body": ""} for key, sha in shas.items()])
