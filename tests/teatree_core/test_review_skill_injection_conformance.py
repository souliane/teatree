"""A headless reviewer must see the overlay's review skills IN FULL.

Root cause this pins: ``build_system_context`` builds the reviewing-phase
system context with ``primary_skills={lifecycle_skill}`` (only ``review`` +
``rules``). Every other skill — including the active overlay's own skill and
its review companions — is demoted by ``_read_skill_contents_scoped`` to a
one-line ``"- <name>: available — load if needed"`` summary. A ``claude -p``
headless reviewer does not auto-call the Skill tool, so the overlay's review
conventions never reach it and it reviews without overlay knowledge.

These tests use a SYNTHETIC overlay that declares a review companion skill
with a sentinel body and assert the sentinel appears in full in the
reviewing-phase system context — RED on ``origin/main`` (the body is demoted
to the summary line), GREEN after the fix embeds the overlay review skills.

They also assert the two backstops the fix threads through:

*   ``OverlayConfig.get_review_companion_skills()`` returns the deduped ordered
    ``[pr_review_companion, *companion_skills]``.
*   ``OverlayConfig.get_lifecycle_companion_skills(lifecycle)`` generalizes that
    to every lifecycle (``review`` keeps the richer set; others get the standing
    companions).
*   ``subagent_skill_gate.required_skills_for_task`` for a review task unions in
    the active overlay's review companions.
*   Back-compat: ``code-review`` (the #1135 default ``pr_review_companion``)
    is still present when no overlay override is declared.
"""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.agents import prompt, skill_injection
from teatree.agents.skill_bundle import active_overlay_lifecycle_skills, active_overlay_review_skills
from teatree.core.models import Session, Task, Ticket
from teatree.core.overlay import OverlayConfig

_SENTINEL = "OVERLAY-REVIEW-SENTINEL: post-funding terminal statuses are tenant-configurable"
_COMPANION_NAME = "overlay-review-conventions"


def _seed_skill(skills_dir: Path, name: str, *, body: str) -> None:
    skill = skills_dir / name
    skill.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        ["---", f"name: {name}", 'description: "Synthetic overlay review skill."', "---", f"# {name}", "", body, ""]
    )
    (skill / "SKILL.md").write_text(content, encoding="utf-8")


@pytest.fixture
def skills_dir(tmp_path: Path) -> Iterator[Path]:
    """Seed a synthetic skills tree and point the prompt module at it."""
    sd = tmp_path / "skills"
    _seed_skill(sd, "review", body="# review lifecycle skill")
    _seed_skill(sd, "rules", body="# rules")
    _seed_skill(sd, _COMPANION_NAME, body=_SENTINEL)
    _seed_skill(sd, "code-review", body="# code-review companion")
    with patch.object(skill_injection, "DEFAULT_SKILLS_DIR", sd):
        yield sd


@pytest.fixture
def overlay_with_review_companion() -> Iterator[MagicMock]:
    """An active overlay whose review companion is the synthetic sentinel skill."""
    overlay = MagicMock()
    overlay.config.companion_skills = []
    overlay.config.pr_review_companion = _COMPANION_NAME
    overlay.config.get_review_companion_skills.return_value = [_COMPANION_NAME]
    with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
        yield overlay


class TestOverlayConfigGetReviewCompanionSkills:
    """``get_review_companion_skills`` = deduped ordered ``[pr_review_companion, *companion_skills]``."""

    def test_default_is_just_code_review(self) -> None:
        config = OverlayConfig()
        assert config.get_review_companion_skills() == ["code-review"]

    def test_prepends_pr_review_companion_to_companion_skills(self) -> None:
        config = OverlayConfig()
        config.pr_review_companion = "primary-review"
        config.companion_skills = ["ac-django", "backend-dev"]
        assert config.get_review_companion_skills() == ["primary-review", "ac-django", "backend-dev"]

    def test_dedupes_overlap_preserving_order(self) -> None:
        config = OverlayConfig()
        config.pr_review_companion = "code-review"
        config.companion_skills = ["ac-django", "code-review"]
        assert config.get_review_companion_skills() == ["code-review", "ac-django"]

    def test_empty_pr_review_companion_is_skipped(self) -> None:
        config = OverlayConfig()
        config.pr_review_companion = ""
        config.companion_skills = ["ac-django"]
        assert config.get_review_companion_skills() == ["ac-django"]


class TestOverlayConfigGetLifecycleCompanionSkills:
    """``get_lifecycle_companion_skills`` generalizes the review companions to every lifecycle."""

    def test_review_lifecycle_returns_review_companion_set(self) -> None:
        config = OverlayConfig()
        config.pr_review_companion = "primary-review"
        config.companion_skills = ["ac-django"]
        assert config.get_lifecycle_companion_skills("review") == ["primary-review", "ac-django"]

    def test_non_review_lifecycle_returns_standing_companions_only(self) -> None:
        config = OverlayConfig()
        config.pr_review_companion = "primary-review"
        config.companion_skills = ["ac-django", "backend-dev"]
        assert config.get_lifecycle_companion_skills("code") == ["ac-django", "backend-dev"]

    def test_non_review_lifecycle_excludes_pr_review_companion(self) -> None:
        config = OverlayConfig()
        config.pr_review_companion = "primary-review"
        config.companion_skills = []
        assert config.get_lifecycle_companion_skills("e2e") == []


class TestActiveOverlayReviewSkills:
    """``active_overlay_review_skills`` mirrors ``active_overlay_pr_review_companion``."""

    def test_reads_overlay_hook(self, overlay_with_review_companion: MagicMock) -> None:
        assert active_overlay_review_skills() == [_COMPANION_NAME]

    def test_no_overlay_returns_empty(self) -> None:
        with patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")):
            assert active_overlay_review_skills() == []


class TestActiveOverlayLifecycleSkills:
    """``active_overlay_lifecycle_skills`` reads the per-lifecycle overlay hook."""

    def test_reads_overlay_lifecycle_hook(self) -> None:
        overlay = MagicMock()
        overlay.config.get_lifecycle_companion_skills.side_effect = lambda lifecycle: [f"{lifecycle}-companion"]
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            assert active_overlay_lifecycle_skills("code") == ["code-companion"]

    def test_no_overlay_returns_empty(self) -> None:
        with patch("teatree.core.overlay_loader.get_overlay", side_effect=RuntimeError("no overlay")):
            assert active_overlay_lifecycle_skills("code") == []

    def test_overlay_without_hook_returns_empty(self) -> None:
        overlay = MagicMock()
        overlay.config = object()
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            assert active_overlay_lifecycle_skills("code") == []


class TestReviewingContextEmbedsOverlayReviewSkillInFull(TestCase):
    """The reviewing-phase system context embeds the overlay review skill IN FULL.

    RED on ``origin/main``: the synthetic companion is demoted to the
    ``"available — load if needed"`` summary line and the sentinel body is
    absent. GREEN after the fix.
    """

    def _review_task(self) -> Task:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="cold-reviewer")
        return Task.objects.create(ticket=ticket, session=session, phase="reviewing")

    @pytest.mark.usefixtures("skills_dir")
    def test_sentinel_body_present_not_summary(self) -> None:
        overlay = MagicMock()
        overlay.config.companion_skills = []
        overlay.config.pr_review_companion = _COMPANION_NAME
        overlay.config.get_review_companion_skills.return_value = [_COMPANION_NAME]
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            context = prompt.build_system_context(
                self._review_task(),
                skills=["review", "rules", _COMPANION_NAME],
                lifecycle_skill="review",
            )
        assert _SENTINEL in context
        assert f"- {_COMPANION_NAME}: available — load if needed" not in context

    @pytest.mark.usefixtures("skills_dir")
    def test_non_reviewing_phase_unchanged(self) -> None:
        """A coding-phase context does NOT embed the overlay review skill in full."""
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="maker:coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        overlay = MagicMock()
        overlay.config.get_review_companion_skills.return_value = [_COMPANION_NAME]
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            context = prompt.build_system_context(
                task,
                skills=["code", "rules", _COMPANION_NAME],
                lifecycle_skill="code",
            )
        assert _SENTINEL not in context


class TestRequiredSkillsForTaskUnionsOverlayLifecycleCompanions:
    """``required_skills_for_task`` unions the overlay's lifecycle companions.

    Generalizes the former review-only union: a ``review`` task pulls in the
    overlay's review companions; a ``debug`` task pulls in the overlay's
    non-review companions but NOT the review-only ones. The companion-resolution
    closure runs against a real fixture skills tree so the union is the
    production resolver's, not a hand-rolled list.
    """

    @pytest.fixture
    def gate_skills(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        sd = tmp_path / "skills"
        _seed_skill(sd, "review", body="# review")
        _seed_skill(sd, "debug", body="# debug")
        _seed_skill(sd, _COMPANION_NAME, body=_SENTINEL)
        _seed_skill(sd, "code-review", body="# code-review")
        monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(sd))
        return sd

    @staticmethod
    def _overlay(*, review: list[str], non_review: list[str]) -> MagicMock:
        overlay = MagicMock()
        overlay.config.get_lifecycle_companion_skills.side_effect = lambda lifecycle: (
            review if lifecycle == "review" else non_review
        )
        return overlay

    def test_review_task_includes_overlay_review_companion(self, gate_skills: Path) -> None:
        from subagent_skill_gate import required_skills_for_task  # noqa: PLC0415

        overlay = self._overlay(review=[_COMPANION_NAME], non_review=[])
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            required = required_skills_for_task("please review the open PR and leave feedback", [gate_skills])
        assert _COMPANION_NAME in required

    def test_back_compat_code_review_present_without_override(self, gate_skills: Path) -> None:
        from subagent_skill_gate import required_skills_for_task  # noqa: PLC0415

        overlay = self._overlay(review=["code-review"], non_review=[])
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            required = required_skills_for_task("please review the open PR and leave feedback", [gate_skills])
        assert "code-review" in required

    def test_non_review_task_does_not_union_review_only_companions(self, gate_skills: Path) -> None:
        from subagent_skill_gate import required_skills_for_task  # noqa: PLC0415

        overlay = self._overlay(review=[_COMPANION_NAME], non_review=[])
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            required = required_skills_for_task("fix the broken parser", [gate_skills])
        assert _COMPANION_NAME not in required
