"""Tests for teatree.agents.phase_blocks — the per-phase trailing context blocks."""

from unittest.mock import patch

from django.test import TestCase

from teatree.agents.phase_blocks import (
    build_reviewer_dispatch_prompt,
    embedded_intake_survey_json,
    intake_survey_json,
    phase_specific_lines,
)
from teatree.agents.review_envelope_recorder import _RUBRIC_GRADED_PHASES
from teatree.core.modelkit.phase_tools import ENVELOPE_VERDICT_PHASES
from teatree.core.modelkit.phases import CANONICAL_PHASES
from teatree.core.modelkit.review_contract import ENVELOPE_FINDINGS_RULE
from teatree.core.models import LandscapeArtifact, Session, Task, Ticket
from teatree.core.models.reviewer_identity import REVIEWER_IDENTITY_INSTRUCTION, assigned_reviewer_identity
from teatree.core.models.types import FIX_RECORD_FIELDS


def _task(phase: str, *, kind: Ticket.Kind = Ticket.Kind.FEATURE) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, kind=kind)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase)


class TestPhaseSpecificLinesDispatch(TestCase):
    """``phase_specific_lines`` maps each canonical phase to exactly one block."""

    def test_unregistered_phase_carries_no_block(self) -> None:
        assert phase_specific_lines(_task("scoping"), []) == ()

    def test_shipping_carries_the_auto_review_gate(self) -> None:
        lines = phase_specific_lines(_task("shipping"), [])
        assert "PHASE: shipping — auto-review gate" in lines


class TestTheReviewerIsToldToGradeTheRubric(TestCase):
    """Nothing else grades the rubric, so an unbriefed reviewer leaves every merge refused.

    The brief teaches the ENVELOPE now, not the shell command. Prose that must be
    remembered across a long review is the failure mode the envelope path replaces:
    the reviewer returns `rubric_grades` beside its verdict and the orchestrator
    stamps them, so grading cannot be forgotten separately from verdicting.
    """

    def _reviewing_brief(self) -> str:
        return "\n".join(phase_specific_lines(_task("reviewing"), []))

    def test_the_reviewing_brief_asks_for_the_grades_in_the_envelope(self) -> None:
        brief = self._reviewing_brief()
        assert "rubric_grades" in brief
        assert "ordinal" in brief
        assert "rationale" in brief

    def test_the_reviewing_brief_requires_every_criterion_graded(self) -> None:
        brief = self._reviewing_brief()
        assert "EVERY criterion" in brief
        assert "records nothing" in brief

    def test_the_reviewing_brief_says_a_pass_cites_a_test_and_a_fail_does_not(self) -> None:
        brief = self._reviewing_brief()
        assert "A PASS cites" in brief
        assert "a FAIL needs none" in brief

    def test_the_reviewing_brief_says_a_recorded_fail_is_a_finding_no_bypass_overrides(self) -> None:
        assert "no bypass overrides" in self._reviewing_brief()

    def test_the_reviewing_brief_sends_the_operator_seam_to_the_operator(self) -> None:
        # The shell command still exists and the phase still carries the shell, so a
        # reviewer that runs it grades OUTSIDE the atomic the recorder holds.
        brief = self._reviewing_brief()
        assert "do NOT run" in brief
        assert "ticket rubric-grade" in brief

    def test_the_reviewing_brief_keeps_the_findings_rule(self) -> None:
        assert ENVELOPE_FINDINGS_RULE in phase_specific_lines(_task("reviewing"), [])

    def test_a_non_reviewing_brief_does_not_carry_it(self) -> None:
        assert "rubric_grades" not in "\n".join(phase_specific_lines(_task("coding"), []))

    def test_every_phase_the_recorder_bills_for_coverage_is_shown_the_checklist(self) -> None:
        # The recorder refuses a verdict leaving a criterion ungraded, so a phase it bills
        # must be one whose brief names the criteria. Adding a phase to the recorder's set
        # without briefing it turns this red.
        for phase in _RUBRIC_GRADED_PHASES:
            assert "rubric_grades" in "\n".join(phase_specific_lines(_task(phase), [])), phase

    def test_e2e_reviewing_is_billed_for_nothing_because_it_is_shown_nothing(self) -> None:
        assert "rubric_grades" not in "\n".join(phase_specific_lines(_task("e2e_reviewing"), []))
        assert "e2e_reviewing" not in _RUBRIC_GRADED_PHASES


class TestFixRecordDirective(TestCase):
    """#4520: the FixRecord directive is KIND-conditional, so a feature brief is unchanged."""

    def _brief(self, phase: str, kind: Ticket.Kind) -> str:
        return "\n".join(phase_specific_lines(_task(phase, kind=kind), []))

    def test_a_fix_ticket_coding_brief_names_every_field(self) -> None:
        brief = self._brief("coding", Ticket.Kind.FIX)
        assert "THIS IS A FIX TICKET" in brief
        for field in FIX_RECORD_FIELDS:
            assert field in brief

    def test_a_fix_ticket_debugging_brief_carries_it_too(self) -> None:
        assert "THIS IS A FIX TICKET" in self._brief("debugging", Ticket.Kind.FIX)

    def test_a_feature_ticket_coding_brief_is_unchanged(self) -> None:
        feature = phase_specific_lines(_task("coding", kind=Ticket.Kind.FEATURE), [])
        assert all("fix_record" not in line for line in feature)

    def test_a_non_fixing_phase_carries_no_directive(self) -> None:
        assert "THIS IS A FIX TICKET" not in self._brief("reviewing", Ticket.Kind.FIX)


class TestTestingPhaseBranchCurrency(TestCase):
    """#2663: a testing dispatch carries the branch-currency verdict, and only it does."""

    _HEADER = "PHASE: testing — branch-currency preflight"

    def test_testing_carries_the_branch_currency_preflight(self) -> None:
        assert self._HEADER in phase_specific_lines(_task("testing"), [])

    def test_the_test_alias_resolves_to_the_same_block(self) -> None:
        assert self._HEADER in phase_specific_lines(_task("test"), [])

    def test_a_worktreeless_ticket_is_loud_rather_than_silently_empty(self) -> None:
        brief = "\n".join(phase_specific_lines(_task("testing"), []))

        assert "UNVERIFIED" in brief
        assert "no ticket worktree materialised at dispatch" in brief

    def test_other_phases_are_unchanged(self) -> None:
        for phase in ("coding", "reviewing", "shipping", "planning"):
            assert self._HEADER not in "\n".join(phase_specific_lines(_task(phase), []))


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


class TestEmbeddedIntakeSurveyJson(TestCase):
    """Only the phase that EMBEDS the survey offers it to the byte-budget pass.

    The pass truncates by exact-substring replace, so a survey handed to it for
    a phase whose block never embedded it is a phantom that can reclaim nothing.
    This is the gate that keeps the two in step — whichever phases
    ``_PHASE_BLOCK_BUILDERS`` grows, a survey is offered iff it is in the text.
    """

    def _recorded(self, phase: str) -> Task:
        task = _task(phase)
        LandscapeArtifact.record(ticket=task.ticket, survey={"a": 1}, recorded_by="t3:intake")
        return task

    def test_planning_offers_the_string_its_block_embeds(self) -> None:
        task = self._recorded("planning")

        assert embedded_intake_survey_json(task) == intake_survey_json(task)
        assert embedded_intake_survey_json(task) in phase_specific_lines(task, [])

    def test_a_phase_that_does_not_embed_the_survey_offers_nothing(self) -> None:
        for phase in ("coding", "testing", "reviewing", "shipping"):
            with self.subTest(phase=phase):
                task = self._recorded(phase)

                assert embedded_intake_survey_json(task) == ""
                assert intake_survey_json(task) not in phase_specific_lines(task, [])


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


class TestEnvelopeVerdictPhasesIsExactlyWhatTheBriefTeaches(TestCase):
    """The pre-dispatch gate refuses on a head only the ENVELOPE path needs.

    A phase whose brief never asks for a ``review_verdict`` records through the shell, off
    the reviewer's own checkout, so gating it would refuse a run that records fine today.
    The set is therefore derived from the brief and pinned here rather than remembered.
    """

    #: The directive ``_REVIEW_VERDICT_RETURN_LINES`` opens with — present iff the brief
    #: asks for the envelope the orchestrator must record server-side.
    _RETURN_DIRECTIVE = "RECORD YOUR VERDICT BY RETURNING IT"

    def _phases_briefed_for_the_envelope(self) -> set[str]:
        return {
            phase
            for phase in CANONICAL_PHASES
            if self._RETURN_DIRECTIVE in "\n".join(phase_specific_lines(_task(phase), []))
        }

    def test_the_gated_set_is_exactly_the_briefed_set(self) -> None:
        assert self._phases_briefed_for_the_envelope() == set(ENVELOPE_VERDICT_PHASES)

    def test_the_shell_recording_review_phases_are_outside_it(self) -> None:
        for phase in ("codex_reviewing", "codex_adversarial_reviewing", "e2e_reviewing"):
            assert phase not in ENVELOPE_VERDICT_PHASES, phase

    def test_the_rubric_graded_set_is_the_same_set_not_a_second_spelling(self) -> None:
        assert _RUBRIC_GRADED_PHASES == ENVELOPE_VERDICT_PHASES


class TestAnsweringWorkItemBlock(TestCase):
    """The ``work_item`` mandate reaches the brief only on a task that owes one (#4527)."""

    def _answering_task(self, *, implies_work: bool) -> Task:
        ticket = Ticket.objects.create(
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.WORK_STARTED,
            extra={"slack_answer": {"slack_ts": "1.0", "question": "q", "implies_work": implies_work}},
        )
        session = Session.objects.create(ticket=ticket, agent_id="answering")
        return Task.objects.create(ticket=ticket, session=session, phase="answering")

    def test_a_work_implying_request_is_told_to_return_a_work_item(self) -> None:
        lines = phase_specific_lines(self._answering_task(implies_work=True), [])

        assert any("work_item" in line for line in lines), (
            "the phase refuses a missing work_item but the brief never asked for one"
        )

    def test_an_ordinary_question_is_not_asked_for_one(self) -> None:
        lines = phase_specific_lines(self._answering_task(implies_work=False), [])

        assert not any("work_item" in line for line in lines)


class TestReviewingBriefAssignsTheIdentity(TestCase):
    """#2663: the brief names the identity, instead of asking the agent to invent one."""

    _PR_ID = 4658

    def _reviewing_brief(self, *, issue_url: str) -> str:
        ticket = Ticket.objects.create(issue_url=issue_url, role=Ticket.Role.REVIEWER, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="reviewing")
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        return "\n".join(phase_specific_lines(task, []))

    def test_a_pr_backed_review_is_handed_the_literal_not_a_template(self) -> None:
        brief = self._reviewing_brief(issue_url=f"https://github.com/souliane/teatree/pull/{self._PR_ID}")
        assert f'"reviewer_identity": "{assigned_reviewer_identity(self._PR_ID)}"' in brief
        assert "<pr-or-task-id>" not in brief

    def test_the_brief_says_the_value_is_assigned_not_chosen(self) -> None:
        brief = self._reviewing_brief(issue_url=f"https://github.com/souliane/teatree/pull/{self._PR_ID}")
        assert "ASSIGNED" in brief

    def test_a_review_answerable_for_no_pr_keeps_the_template(self) -> None:
        # Control: nothing to derive an identity from, so the self-naming instruction stays.
        brief = self._reviewing_brief(issue_url="https://github.com/souliane/teatree/issues/2663")
        assert REVIEWER_IDENTITY_INSTRUCTION in brief


class TestReviewingGreenProofBinding(TestCase):
    """#4720: a reviewing brief binds `verify-gates` to the head, so it cannot grade the default branch."""

    _HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

    def _reviewer_task(self, *, issue_url: str, reviewed_sha: str) -> Task:
        ticket = Ticket.objects.create(
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.WORK_STARTED,
            issue_url=issue_url,
            extra={"reviewed_sha": reviewed_sha} if reviewed_sha else {},
        )
        session = Session.objects.create(ticket=ticket, agent_id="reviewing")
        return Task.objects.create(ticket=ticket, session=session, phase="reviewing")

    def _brief(self, **kwargs: str) -> str:
        return "\n".join(phase_specific_lines(self._reviewer_task(**kwargs), []))

    def test_brief_names_the_head_and_the_expect_sha_command(self) -> None:
        brief = self._brief(issue_url="https://github.com/o/r/pull/42", reviewed_sha=self._HEAD)
        assert f"GREEN-PROOF BINDING: the head under review is {self._HEAD}." in brief
        assert f"t3 tool verify-gates --expect-sha {self._HEAD}" in brief
        assert "https://github.com/o/r/pull/42" in brief

    def test_brief_names_the_forge_check_runs_as_the_merge_authority(self) -> None:
        assert "check-runs" in self._brief(issue_url="https://github.com/o/r/pull/42", reviewed_sha=self._HEAD)

    def test_no_recorded_head_carries_no_binding(self) -> None:
        assert "GREEN-PROOF BINDING" not in self._brief(issue_url="https://github.com/o/r/pull/42", reviewed_sha="")

    def test_a_ticket_answerable_for_no_pull_request_carries_no_binding(self) -> None:
        assert "GREEN-PROOF BINDING" not in self._brief(
            issue_url="https://github.com/o/r/issues/42", reviewed_sha=self._HEAD
        )

    def test_a_coding_brief_carries_no_binding(self) -> None:
        assert "GREEN-PROOF BINDING" not in "\n".join(phase_specific_lines(_task("coding"), []))
