"""Worktree-scoped skill/overlay resolution — the PR-12 dispatch-preflight seam.

A dispatched task runs in its OWN worktree, so ``resolve_skill_bundle`` must
detect framework + overlay skills from the worktree path, never the
orchestrator's ambient cwd (the loop's clone). These pin the threading and the
fall-back.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.agents import skill_bundle
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.skill_support.loading import SkillLoadingPolicy


def _spy_on_cwd(captured: dict[str, Path]) -> object:
    real = SkillLoadingPolicy.select_for_runtime_phase

    def _spy(self: SkillLoadingPolicy, *, cwd: Path, **kwargs: object) -> object:
        captured["cwd"] = cwd
        return real(self, cwd=cwd, **kwargs)

    return _spy


class TestResolveSkillBundleWorktreeScoping(TestCase):
    def test_detects_framework_skill_from_worktree_not_cwd(self) -> None:
        # A worktree that looks like a Django repo resolves ac-django even when
        # the ambient cwd is not a Django repo — the anti-vacuous proof the
        # detection root is the worktree, not Path.cwd().
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "manage.py").write_text("# django project marker\n")
            bundle = resolve_skill_bundle(
                phase="coding",
                overlay_skill_metadata={},
                worktree_path=tmp,
            )
        assert "ac-django" in bundle

    def test_threads_worktree_path_as_detection_cwd(self) -> None:
        captured: dict[str, Path] = {}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(SkillLoadingPolicy, "select_for_runtime_phase", _spy_on_cwd(captured)),
        ):
            resolve_skill_bundle(phase="coding", overlay_skill_metadata={}, worktree_path=tmp)
        assert captured["cwd"] == Path(tmp)

    def test_falls_back_to_ambient_cwd_when_no_worktree(self) -> None:
        captured: dict[str, Path] = {}
        with patch.object(SkillLoadingPolicy, "select_for_runtime_phase", _spy_on_cwd(captured)):
            resolve_skill_bundle(phase="coding", overlay_skill_metadata={}, worktree_path=None)
        assert captured["cwd"] == Path.cwd()

    def test_missing_worktree_dir_falls_back_to_ambient_cwd(self) -> None:
        # A recorded path that no longer exists on disk must not become the
        # detection root — the loop's cwd is the safe fallback.
        captured: dict[str, Path] = {}
        with patch.object(SkillLoadingPolicy, "select_for_runtime_phase", _spy_on_cwd(captured)):
            resolve_skill_bundle(
                phase="coding",
                overlay_skill_metadata={},
                worktree_path="/nonexistent/worktree/path",
            )
        assert captured["cwd"] == Path.cwd()

    def test_dispatch_cwd_is_the_single_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            assert skill_bundle._dispatch_cwd(tmp) == Path(tmp)
        assert skill_bundle._dispatch_cwd(None) == Path.cwd()
        assert skill_bundle._dispatch_cwd("") == Path.cwd()
