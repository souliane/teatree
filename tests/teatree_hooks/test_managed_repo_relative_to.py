"""``repo_root_is_teatree_managed`` skips a non-subpath base instead of crashing.

Regression for the ``Path.relative_to`` ``ValueError`` bug: the loop that tests
each managed overlay base suppressed only ``OSError`` / ``RuntimeError``, but
``relative_to`` raises ``ValueError`` when the repo is NOT under a base — the
normal case as soon as more than one overlay registers a base (a teatree clone
plus a second overlay's ``path``). When a non-matching base was iterated
FIRST, the unsuppressed ``ValueError`` crashed the classifier; the crash bubbled
out of ``handle_block_main_clone_mutation`` and the router caught it and failed
the main-clone guard OPEN — silently disabling it on every teatree-clone git /
edit call.
"""

from pathlib import Path

import pytest

from hooks.scripts import managed_repo


class TestRepoRootIsTeatreeManagedNonSubpathBase:
    """The managed-base loop must survive a base the repo is not under."""

    def test_non_subpath_base_first_finds_later_match_without_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-subpath base iterated first is skipped; a later matching base wins.

        Pre-fix this raised ``ValueError`` on the first ``relative_to`` and never
        reached the matching base — the exact two-overlay crash.
        """
        non_subpath_base = tmp_path / "other-overlay"
        managed_base = tmp_path / "teatree"
        repo_root = managed_base / "repo"
        repo_root.mkdir(parents=True)
        non_subpath_base.mkdir()

        # Ordering is the whole point: the base the repo is NOT under comes first.
        monkeypatch.setattr(
            managed_repo,
            "overlay_managed_repo_signals",
            lambda: (["souliane/teatree"], [non_subpath_base.resolve(), managed_base.resolve()]),
        )

        assert managed_repo.repo_root_is_teatree_managed(str(repo_root)) is True

    def test_no_matching_base_returns_false_without_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo under NONE of the bases falls through to False, never raises.

        Every base raises ``ValueError`` for this repo; the loop must exhaust
        cleanly (fail OPEN → unmanaged → ``False``) rather than crash.
        """
        base_one = tmp_path / "overlay-a"
        base_two = tmp_path / "overlay-b"
        unmanaged_repo = tmp_path / "elsewhere" / "repo"
        base_one.mkdir()
        base_two.mkdir()
        unmanaged_repo.mkdir(parents=True)

        monkeypatch.setattr(
            managed_repo,
            "overlay_managed_repo_signals",
            lambda: (["souliane/teatree"], [base_one.resolve(), base_two.resolve()]),
        )

        assert managed_repo.repo_root_is_teatree_managed(str(unmanaged_repo)) is False
