"""The scratch walk reads a tree, never what a symlink inside it points at (#5615)."""

import os
from pathlib import Path

from teatree.core.retention.scratch_tree import tree_stats

_LONG_AGO = 1_600_000_000


def test_a_symlinked_directory_is_not_walked_into(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / ".git").mkdir(parents=True)
    (outside / "fresh.log").write_bytes(b"x" * 4096)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside, target_is_directory=True)
    os.utime(tree / "link", (_LONG_AGO, _LONG_AGO), follow_symlinks=False)
    os.utime(tree, (_LONG_AGO, _LONG_AGO))

    stats = tree_stats(tree)

    assert not stats.holds_git_repo
    assert stats.newest_mtime == _LONG_AGO
    assert stats.size_bytes == 0
