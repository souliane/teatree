"""``t3 review merge-tree`` — the CLI seam over the one-step merge-result extract.

Producing a merge result used to be a four-command recipe, and every step was a
chance to skip it and probe the branch instead — which is how a docs-only PR
was blocked by a ``src/`` finding about code it does not touch (#4251). These
pin the runnable entry point: a clean merge prints the extract, a conflicting
one exits 1 with structured JSON and materialises nothing.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli.review import review_app
from tests._git_repo import make_git_repo, run_git

_ORIGIN_URL = "https://github.com/souliane/teatree.git"


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """``main`` widens a grant in ``tool.py``; ``feature`` only touches ``guide.md``."""
    repo = make_git_repo(tmp_path / "clone", initial_commit=False)
    run_git(repo, "remote", "add", "origin", _ORIGIN_URL)
    (repo / "guide.md").write_text("v1\n")
    (repo / "tool.py").write_text("GRANT = ()\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "base")

    run_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "guide.md").write_text("v2\n")
    run_git(repo, "commit", "-q", "-am", "docs only")

    run_git(repo, "checkout", "-q", "main")
    (repo / "tool.py").write_text("GRANT = ('write',)\n")
    run_git(repo, "commit", "-q", "-am", "widen the grant on main")
    return repo


def _invoke(clone: Path, tmp_path: Path, *extra: str) -> tuple[int, str]:
    argv = ["merge-tree", "--repo", str(clone), "--base", "main", "--head", "feature"]
    argv += ["--into", str(tmp_path / "out"), *extra]
    result = CliRunner().invoke(review_app, argv)
    return result.exit_code, result.stdout


class TestMergeTreeCommand:
    def test_it_prints_an_extract_carrying_both_sides(self, clone: Path, tmp_path: Path) -> None:
        code, out = _invoke(clone, tmp_path)

        assert code == 0
        extract = json.loads(out)
        assert Path(extract["path"], "guide.md").read_text(encoding="utf-8") == "v2\n"
        assert Path(extract["path"], "tool.py").read_text(encoding="utf-8") == "GRANT = ('write',)\n"

    def test_the_extract_is_never_a_git_worktree(self, clone: Path, tmp_path: Path) -> None:
        _, out = _invoke(clone, tmp_path)

        assert Path(json.loads(out)["path"], ".git").is_dir()

    def test_a_conflicting_merge_exits_one_and_extracts_nothing(self, clone: Path, tmp_path: Path) -> None:
        run_git(clone, "checkout", "-q", "feature")
        (clone / "tool.py").write_text("GRANT = ('edit',)\n")
        run_git(clone, "commit", "-q", "-am", "rival grant")
        run_git(clone, "checkout", "-q", "main")

        code, out = _invoke(clone, tmp_path)

        assert code == 1
        assert json.loads(out)["error"] == "merge_conflict"
        assert not Path(tmp_path / "out").exists()
