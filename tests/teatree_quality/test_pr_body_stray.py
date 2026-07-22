"""Tests for the stray PR-body detection logic (#3581).

A hand-named ``pr-body.*`` / ``pr_body.*`` file copied into a worktree and staged
is committable junk. :mod:`teatree.quality.pr_body_stray` names such files from
the staged path list so the ``check_pr_body_stray`` gate can refuse the commit.
"""

import pytest

from teatree.quality.pr_body_stray import block_message, is_stray_pr_body, stray_pr_body_paths


class TestIsStrayPrBody:
    @pytest.mark.parametrize(
        "path",
        [
            "pr-body.md",
            "pr_body.md",
            "PR-BODY.md",
            "pr-body-3537.md",
            "pr_body-3541.txt",
            "sub/dir/pr-body.md",
            "pr-body",
        ],
    )
    def test_matches_hand_named_body_files(self, path: str) -> None:
        assert is_stray_pr_body(path)

    @pytest.mark.parametrize(
        "path",
        [
            "src/teatree/utils/pr_body.py",  # the helper module — never flagged
            "tests/teatree_utils/test_pr_body.py",  # its mirror test
            "my_pr_body_helper.py",
            "README.md",
            "docs/pr-guide.md",
        ],
    )
    def test_ignores_source_and_unrelated_files(self, path: str) -> None:
        assert not is_stray_pr_body(path)

    def test_python_source_named_pr_body_is_never_flagged(self) -> None:
        # A PR body is a scratch text file, never Python source. The ``.py``
        # carve-out keeps the ``pr_body.py`` module itself committable.
        assert not is_stray_pr_body("pr_body.py")
        assert not is_stray_pr_body("src/x/pr-body.py")


class TestStrayPrBodyPaths:
    def test_returns_only_the_stray_paths_in_order(self) -> None:
        staged = [
            "src/teatree/backends/github/client.py",
            "pr-body.md",
            "tests/test_x.py",
            "pr_body-3541.md",
        ]
        assert stray_pr_body_paths(staged) == ["pr-body.md", "pr_body-3541.md"]

    def test_empty_when_nothing_stray(self) -> None:
        assert stray_pr_body_paths(["a.py", "b.md"]) == []


class TestBlockMessage:
    def test_names_every_offending_path_and_the_fix(self) -> None:
        message = block_message(["pr-body.md", "pr_body.md"])
        assert "pr-body.md" in message
        assert "pr_body.md" in message
        assert "pr_body_tempfile" in message
        assert "pr create" in message
