"""Worktree-scoped skill/overlay resolution — the PR-12 dispatch-preflight seam.

A dispatched task runs in its OWN worktree, so ``resolve_skill_bundle`` must
detect framework + overlay skills from the worktree path, never the
orchestrator's ambient cwd (the loop's clone). These pin the threading and the
fall-back.
"""

import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents import skill_bundle
from teatree.agents.skill_bundle import (
    ArchitecturalReviewSkillMissingError,
    resolve_skill_bundle,
    stage_skills_for_dispatch,
)
from teatree.config.settings import UserSettings
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
        # detection root is the worktree, not Path.cwd(). Path.cwd() is pinned to
        # a marker-free dir so the ambient teatree repo root (which also has
        # manage.py) can't make this pass with the worktree threading reverted.
        with (
            tempfile.TemporaryDirectory() as worktree,
            tempfile.TemporaryDirectory() as ambient,
            patch.object(Path, "cwd", return_value=Path(ambient)),
        ):
            (Path(worktree) / "manage.py").write_text("# django project marker\n")
            bundle = resolve_skill_bundle(
                phase="coding",
                overlay_skill_metadata={},
                worktree_path=worktree,
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


class TestResolveSkillBundleStageSkillThreading(TestCase):
    def test_threaded_stage_skills_bypass_internal_resolution(self) -> None:
        # #3206: when the dispatch pre-resolves the overlay stage skills and
        # threads them in, resolve_skill_bundle must not re-resolve them.
        captured: dict[str, object] = {}

        def _spy(self: SkillLoadingPolicy, *, stage_skills: object, **kwargs: object) -> object:
            captured["stage_skills"] = stage_skills
            return SimpleNamespace(skills=[])

        with (
            patch("teatree.agents.skill_bundle.active_overlay_stage_skills") as resolver,
            patch.object(SkillLoadingPolicy, "select_for_runtime_phase", _spy),
        ):
            resolve_skill_bundle(
                phase="coding",
                overlay_skill_metadata={},
                stage_skills=["backend-dev"],
            )
        resolver.assert_not_called()
        assert captured["stage_skills"] == ["backend-dev"]

    def test_resolves_internally_when_not_threaded(self) -> None:
        with (
            patch("teatree.agents.skill_bundle.active_overlay_stage_skills", return_value=["x"]) as resolver,
            patch.object(SkillLoadingPolicy, "select_for_runtime_phase", return_value=SimpleNamespace(skills=[])),
        ):
            resolve_skill_bundle(phase="coding", overlay_skill_metadata={})
        resolver.assert_called_once_with("coding")


class TestArchitecturalReviewSkillReachesTheBundle(TestCase):
    """``architectural_review`` is scanner-dispatched, so nothing else declares its skill.

    It is in neither ``SUBAGENT_BY_PHASE`` nor the phase->skill map, so
    ``declared_skills_for_phase`` returns nothing and the configured skill reached the
    model as prose in the task description while its body never entered the bundle.
    """

    def _staged(self, name: str) -> list[Path]:
        staged = Path(tempfile.mkdtemp())
        (staged / name).mkdir()
        (staged / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        return [staged]

    def test_the_configured_skill_is_appended_for_the_review_phase(self) -> None:
        with (
            patch.object(skill_bundle, "active_overlay_stage_skills", return_value=["overlay-x"]),
            patch.object(skill_bundle, "_skill_body_dirs", return_value=self._staged("ac-reviewing-codebase")),
            patch("teatree.config.get_effective_settings", return_value=UserSettings()),
        ):
            assert stage_skills_for_dispatch("architectural_review") == ["overlay-x", "ac-reviewing-codebase"]

    def test_every_other_phase_is_untouched(self) -> None:
        with patch.object(skill_bundle, "active_overlay_stage_skills", return_value=["overlay-x"]):
            assert stage_skills_for_dispatch("coding") == ["overlay-x"]

    def test_an_unresolvable_skill_refuses_with_the_name(self) -> None:
        # The embed drops an unresolvable name silently, so dispatching would run the
        # review with no guidance at all — worse than not running it.
        settings = replace(UserSettings(), architectural_review_skill="ac-reviewing-skills")
        with (
            patch.object(skill_bundle, "active_overlay_stage_skills", return_value=[]),
            patch.object(skill_bundle, "_skill_body_dirs", return_value=self._staged("ac-reviewing-codebase")),
            patch("teatree.config.get_effective_settings", return_value=settings),
            pytest.raises(ArchitecturalReviewSkillMissingError, match="ac-reviewing-skills"),
        ):
            stage_skills_for_dispatch("architectural_review")
