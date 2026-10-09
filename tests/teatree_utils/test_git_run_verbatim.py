r"""``run_strict_verbatim`` keeps the bytes ``run_strict`` strips (#4435).

The salvage bundles were captured through ``run_strict``, whose ``.stdout.strip()``
left every patch unappliable — ``git apply`` reports ``corrupt patch``. These pin
the primitive against the two shapes that made the loss worse than one byte: a
patch ending in a blank CONTEXT line (``.strip()`` eats the line, not just the
newline, so re-appending ``\n`` yields a differently corrupt patch), and ``-z``
porcelain, whose records are column-fixed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from teatree.utils.git_run import run_strict, run_strict_verbatim

_GIT = shutil.which("git") or "/usr/bin/git"
_DIFF_ARGS = ["diff", "HEAD", "--binary", "--src-prefix=a/", "--dst-prefix=b/"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run([_GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose working-tree diff ENDS on a blank context line."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "t@t")
    _git(checkout, "config", "user.name", "t")
    (checkout / "tracked.py").write_text("value = 1\n\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "initial")
    (checkout / "tracked.py").write_text("value = 2\n\n", encoding="utf-8")
    return checkout


class TestVerbatimKeepsTheTrailingBytes:
    def test_the_patch_ends_with_the_blank_context_line_and_its_newline(self, repo: Path) -> None:
        patch = run_strict_verbatim(repo=str(repo), args=_DIFF_ARGS)

        assert patch.endswith(" \n"), repr(patch[-20:])

    def test_run_strict_eats_the_whole_blank_context_line_not_just_the_newline(self, repo: Path) -> None:
        verbatim = run_strict_verbatim(repo=str(repo), args=_DIFF_ARGS)
        stripped = run_strict(repo=str(repo), args=_DIFF_ARGS)

        assert stripped == verbatim.rstrip(), "run_strict eats every trailing whitespace byte, not one newline"
        assert stripped + "\n" != verbatim, (
            "a whole trailing line went with it, so the issue's one-newline repair cannot reconstruct the patch"
        )

    def test_the_verbatim_patch_applies_where_the_stripped_one_is_corrupt(self, repo: Path, tmp_path: Path) -> None:
        restore = tmp_path / "restore"
        _git(repo, "worktree", "add", "-q", "--detach", str(restore), "HEAD")
        verbatim = run_strict_verbatim(repo=str(repo), args=_DIFF_ARGS)

        outcomes = {}
        for label, text in (("stripped", run_strict(repo=str(repo), args=_DIFF_ARGS)), ("verbatim", verbatim)):
            patch_file = tmp_path / f"{label}.diff"
            patch_file.write_text(text, encoding="utf-8")
            outcomes[label] = subprocess.run(
                [_GIT, "-C", str(restore), "apply", str(patch_file)], capture_output=True, text=True, check=False
            ).returncode

        assert outcomes["verbatim"] == 0
        assert outcomes["stripped"] != 0, "control: the stripped patch must be the one git apply rejects"


class TestVerbatimIsWhatZPorcelainNeeds:
    def test_an_unstaged_only_entry_keeps_its_leading_status_column(self, repo: Path) -> None:
        record = run_strict_verbatim(repo=str(repo), args=["status", "--porcelain", "-z"]).split("\0")[0]

        assert record == " M tracked.py", repr(record)
