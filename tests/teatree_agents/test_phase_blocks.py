"""Tests for teatree.agents.phase_blocks — the per-phase trailing context blocks."""

from unittest.mock import patch

from django.test import TestCase

from teatree.agents.phase_blocks import build_reviewer_dispatch_prompt, intake_survey_json, phase_specific_lines
from teatree.core.models import LandscapeArtifact, Session, Task, Ticket


def _task(phase: str) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase)


class TestPhaseSpecificLinesDispatch(TestCase):
    """``phase_specific_lines`` maps each canonical phase to exactly one block."""

    def test_unregistered_phase_carries_no_block(self) -> None:
        assert phase_specific_lines(_task("scoping"), []) == ()

    def test_shipping_carries_the_auto_review_gate(self) -> None:
        lines = phase_specific_lines(_task("shipping"), [])
        assert "PHASE: shipping — auto-review gate" in lines


class TestIntakeSurveyJson(TestCase):
    """The survey substring the byte-budget pass re-derives to truncate.

    ``_enforce_context_budget`` re-derives it to locate the exact block to
    elide, so it must render byte-identically to what the planner block
    embedded — a divergence would make the budget pass truncate nothing.
    """

    def test_no_recorded_survey_renders_empty(self) -> None:
        assert intake_survey_json(_task("planning")) == ""

    def test_recorded_survey_renders_deterministic_json(self) -> None:
        task = _task("planning")
        LandscapeArtifact.record(ticket=task.ticket, survey={"b": 2, "a": 1}, recorded_by="t3:intake")

        assert intake_survey_json(task) == '{"a": 1, "b": 2}'

    def test_planner_block_embeds_the_same_string(self) -> None:
        task = _task("planning")
        LandscapeArtifact.record(ticket=task.ticket, survey={"prs": []}, recorded_by="t3:intake")

        assert intake_survey_json(task) in phase_specific_lines(task, [])


class TestBuildReviewerDispatchPrompt(TestCase):
    """The shared reviewer dispatch-prompt builder embeds the overlay review skills.

    A review sub-agent dispatched via the Agent tool / a dynamic workflow /
    a reviewer structurally loads them through the REQUIRED load
    block instead of relying on the orchestrator to remember.
    """

    def test_review_instruction_is_present(self) -> None:
        with patch("teatree.agents.skill_bundle.active_overlay_review_skills", return_value=[]):
            out = build_reviewer_dispatch_prompt(review_instruction="Review the diff on branch foo")
        assert "Review the diff on branch foo" in out

    def test_lifecycle_review_skill_always_required(self) -> None:
        with patch("teatree.agents.skill_bundle.active_overlay_review_skills", return_value=[]):
            out = build_reviewer_dispatch_prompt(review_instruction="x")
        assert "/t3:review" in out
        assert "Skill tool" in out

    def test_overlay_review_skills_resolved_and_required(self) -> None:
        with patch(
            "teatree.agents.skill_bundle.active_overlay_review_skills",
            return_value=["code-review", "ac-reviewing-codebase"],
        ):
            out = build_reviewer_dispatch_prompt(review_instruction="x")
        assert "/code-review" in out
        assert "/ac-reviewing-codebase" in out

    def test_explicit_review_skills_override_overlay_resolution(self) -> None:
        with patch("teatree.agents.skill_bundle.active_overlay_review_skills", return_value=["should-not-appear"]):
            out = build_reviewer_dispatch_prompt(review_instruction="x", review_skills=["explicit-skill"])
        assert "/explicit-skill" in out
        assert "should-not-appear" not in out

    def test_skills_deduped_and_lifecycle_not_duplicated(self) -> None:
        out = build_reviewer_dispatch_prompt(
            review_instruction="x", review_skills=["t3:review", "code-review", "code-review"]
        )
        assert out.count("/code-review") == 1
        assert out.count("/t3:review") == 1

    def test_load_block_precedes_instruction(self) -> None:
        with patch("teatree.agents.skill_bundle.active_overlay_review_skills", return_value=["code-review"]):
            out = build_reviewer_dispatch_prompt(review_instruction="REVIEW-BODY-MARKER")
        assert out.index("/code-review") < out.index("REVIEW-BODY-MARKER")
