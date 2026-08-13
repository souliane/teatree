"""``extract_merge_result`` materialises the merge tree in a plain directory (#4251).

Drives real ``git`` under ``tmp_path``: two branches touching different files,
extracted, then asserted to carry BOTH sides. The environment assertions are
the point — a git worktree auto-isolates onto a per-worktree DB, and a clone
whose ``origin`` is a local path silently changes what repo-identity reads
resolve, so both have produced confident wrong answers on this repo.
"""

from pathlib import Path

import pytest

from teatree.utils.merge_tree_extract import MergeTreeConflictError, extract_merge_result
from teatree.utils.run import run_checked

_ORIGIN_URL = "https://github.com/souliane/teatree.git"


def _git(repo: Path, *args: str) -> str:
    return run_checked(["git", "-C", str(repo), *args]).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone with ``main`` (docs + src) and a docs-only branch off it."""
    root = tmp_path / "clone"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "remote", "add", "origin", _ORIGIN_URL)
    (root / "guide.md").write_text("v1\n")
    (root / "tool.py").write_text("GRANT = ()\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")

    _git(root, "checkout", "-b", "docs-only")
    (root / "guide.md").write_text("v2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "docs")

    _git(root, "checkout", "main")
    (root / "tool.py").write_text("GRANT = ('write',)\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "widen the grant on main")
    return root


class TestExtractMergeResult:
    def test_the_extract_carries_both_sides_of_the_merge(self, repo: Path, tmp_path: Path) -> None:
        extract = extract_merge_result(str(repo), base="main", head="docs-only", into=str(tmp_path / "out"))

        assert Path(extract.path, "guide.md").read_text(encoding="utf-8") == "v2\n"
        assert Path(extract.path, "tool.py").read_text(encoding="utf-8") == "GRANT = ('write',)\n"

    def test_the_extract_is_a_primary_checkout_never_a_git_worktree(self, repo: Path, tmp_path: Path) -> None:
        # `resolve_data_dir` auto-isolates a WORKTREE onto a per-worktree DB — the
        # trap that produced a second false result the same evening.
        extract = extract_merge_result(str(repo), base="main", head="docs-only", into=str(tmp_path / "out"))

        assert Path(extract.path, ".git").is_dir()
        assert not Path(extract.path, ".git").is_file()

    def test_the_extract_inherits_the_source_clone_real_origin_url(self, repo: Path, tmp_path: Path) -> None:
        extract = extract_merge_result(str(repo), base="main", head="docs-only", into=str(tmp_path / "out"))

        assert _git(Path(extract.path), "remote", "get-url", "origin") == _ORIGIN_URL

    def test_no_git_leaves_a_bare_directory(self, repo: Path, tmp_path: Path) -> None:
        extract = extract_merge_result(
            str(repo), base="main", head="docs-only", into=str(tmp_path / "out"), init_git=False
        )

        assert not Path(extract.path, ".git").exists()
        assert Path(extract.path, "guide.md").read_text(encoding="utf-8") == "v2\n"

    def test_it_reports_the_merged_tree_and_the_resolved_ends(self, repo: Path, tmp_path: Path) -> None:
        extract = extract_merge_result(str(repo), base="main", head="docs-only", into=str(tmp_path / "out"))

        assert extract.tree_oid == _git(repo, "merge-tree", "--write-tree", "main", "docs-only")
        assert extract.base_sha == _git(repo, "rev-parse", "main")
        assert extract.head_sha == _git(repo, "rev-parse", "docs-only")

    def test_a_conflicting_merge_raises_rather_than_extracting_a_wrong_tree(self, repo: Path, tmp_path: Path) -> None:
        _git(repo, "checkout", "-b", "rival", "docs-only")
        (repo / "tool.py").write_text("GRANT = ('edit',)\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "rival grant")
        _git(repo, "checkout", "main")

        with pytest.raises(MergeTreeConflictError) as excinfo:
            extract_merge_result(str(repo), base="main", head="rival", into=str(tmp_path / "out"))

        assert "tool.py" in str(excinfo.value)
        assert not Path(tmp_path / "out").exists()
