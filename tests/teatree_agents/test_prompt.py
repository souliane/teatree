"""Tests for teatree.agents.prompt — agent prompt building."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.agents.prompt import (
    _is_primary,
    _parent_result_summary,
    _read_skill_contents,
    _read_skill_contents_scoped,
    build_interactive_context,
    build_reviewer_dispatch_prompt,
    build_system_context,
    build_task_prompt,
)
from teatree.core.models import Session, Task, TaskAttempt, Ticket

# --- _read_skill_contents ---


def test_read_skill_contents_reads_existing_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# My Skill\nDo stuff.", encoding="utf-8")

    result = _read_skill_contents(["my-skill"], skills_dir=tmp_path)
    assert "--- SKILL: my-skill ---" in result
    assert "# My Skill" in result


def test_read_skill_contents_skips_missing_skill(tmp_path: Path) -> None:
    result = _read_skill_contents(["nonexistent"], skills_dir=tmp_path)
    assert result == ""


def test_read_skill_contents_multiple_skills(tmp_path: Path) -> None:
    for name in ("skill-a", "skill-b"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name}", encoding="utf-8")

    result = _read_skill_contents(["skill-a", "skill-b"], skills_dir=tmp_path)
    assert "--- SKILL: skill-a ---" in result
    assert "--- SKILL: skill-b ---" in result


# --- _is_primary ---


def test_is_primary_matches_short_name() -> None:
    assert _is_primary("test", {"test"})
    assert not _is_primary("code", {"test"})


def test_is_primary_matches_always_full() -> None:
    assert _is_primary("rules", set())


def test_is_primary_matches_absolute_path() -> None:
    assert _is_primary("/tmp/skills/test/SKILL.md", {"test"})
    assert _is_primary("/tmp/skills/rules/SKILL.md", set())
    assert not _is_primary("/tmp/skills/ac-django/SKILL.md", {"test"})


# --- _read_skill_contents_scoped ---


def test_read_scoped_embeds_primary_and_summarizes_companions(tmp_path: Path) -> None:
    for name in ("rules", "test", "ac-django", "workspace"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name} full content", encoding="utf-8")

    result = _read_skill_contents_scoped(
        ["ac-django", "workspace", "rules", "test"],
        primary_skills={"test"},
        skills_dir=tmp_path,
    )
    # Primary skills get full content
    assert "--- SKILL: test ---" in result
    assert "# test full content" in result
    # rules is always fully loaded
    assert "--- SKILL: rules ---" in result
    assert "# rules full content" in result
    # Companion skills get summary only
    assert "COMPANION SKILLS" in result
    assert "- ac-django:" in result
    assert "- workspace:" in result
    assert "# ac-django full content" not in result
    assert "# workspace full content" not in result


def test_read_scoped_all_primary(tmp_path: Path) -> None:
    d = tmp_path / "only-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("# Only", encoding="utf-8")

    result = _read_skill_contents_scoped(
        ["only-skill"],
        primary_skills={"only-skill"},
        skills_dir=tmp_path,
    )
    assert "--- SKILL: only-skill ---" in result
    assert "COMPANION" not in result


def test_read_scoped_missing_skill(tmp_path: Path) -> None:
    result = _read_skill_contents_scoped(
        ["nonexistent"],
        primary_skills={"nonexistent"},
        skills_dir=tmp_path,
    )
    assert result == ""


# --- build_task_prompt ---


class TestBuildTaskPrompt(TestCase):
    def test_basic(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/42")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "42" in prompt
        assert "https://example.com/issues/42" in prompt

    def test_includes_title_and_labels(self) -> None:
        ticket = Ticket.objects.create(
            issue_url="https://example.com/issues/1",
            extra={"issue_title": "Fix the bug", "labels": ["bug", "urgent"]},
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "Fix the bug" in prompt
        assert "bug, urgent" in prompt

    def test_includes_phase_and_reason(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_reason="Auto-scheduled review",
        )

        prompt = build_task_prompt(task)
        assert "reviewing" in prompt
        assert "Auto-scheduled review" in prompt

    def test_includes_pr_context(self) -> None:
        ticket = Ticket.objects.create(
            extra={
                "prs": {
                    "backend": {
                        "url": "https://gitlab.com/mr/1",
                        "title": "Backend changes",
                        "draft": True,
                        "pipeline_status": "success",
                    },
                },
            },
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "https://gitlab.com/mr/1" in prompt
        assert "(draft)" in prompt
        assert "pipeline: success" in prompt
        assert "Backend changes" in prompt

    def test_skips_non_dict_pr_items(self) -> None:
        ticket = Ticket.objects.create(
            extra={"prs": {"bad": "not-a-dict", "good": {"url": "https://x.com/mr/2"}}},
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "https://x.com/mr/2" in prompt

    def test_handles_non_dict_extra(self) -> None:
        ticket = Ticket.objects.create(extra="not-a-dict")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "Work on ticket" in prompt

    def test_pr_without_title_or_pipeline(self) -> None:
        ticket = Ticket.objects.create(
            extra={"prs": {"repo": {"url": "https://x.com/mr/3", "draft": False}}},
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "https://x.com/mr/3" in prompt
        assert "(draft)" not in prompt
        assert "pipeline:" not in prompt

    def test_non_dict_prs_ignored(self) -> None:
        ticket = Ticket.objects.create(extra={"prs": "not-a-dict"})
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "pull requests" not in prompt.lower()


# --- build_system_context ---


class TestBuildSystemContext(TestCase):
    def test_basic(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/10")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_system_context(task, skills=[])
        assert "TeaTree headless agent" in ctx
        assert "10" in ctx
        assert "/t3:next" in ctx

    def test_with_skills(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp())
        skill_dir = tmp_dir / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Test Skill Content", encoding="utf-8")

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_system_context(task, skills=["test-skill"])
        # skill content is read from default skills_dir, not tmp_dir — so skill will not be found.
        # The test verifies the code path is exercised (lines 78-81).
        assert "TeaTree headless agent" in ctx

    def test_reviewing_phase(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")

        ctx = build_system_context(task, skills=[])
        assert "PHASE: reviewing" in ctx
        assert "code review" in ctx

    def test_planning_phase_injects_persisted_intake_survey(self) -> None:
        # #2541: the planner CONSUMES the survey the intake FSM step persisted —
        # it appears in the planning context, so the planner does not re-derive it.
        from teatree.core.models import LandscapeArtifact  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="planning")
        survey = {"open_prs": [{"url": "https://forge/pr/77"}], "worktrees": [], "recommendations": [], "warnings": []}
        LandscapeArtifact.record(ticket=ticket, survey=survey, recorded_by="t3:intake")

        ctx = build_system_context(task, skills=[])
        assert "INTAKE LANDSCAPE SURVEY" in ctx
        assert "https://forge/pr/77" in ctx

    def test_planning_phase_omits_survey_block_when_none_persisted(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="planning")

        ctx = build_system_context(task, skills=[])
        assert "INTAKE LANDSCAPE SURVEY" not in ctx

    def test_shipping_phase_embeds_reviewer_dispatch_skill_block(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="shipping")

        with patch("teatree.agents.skill_bundle.active_overlay_review_skills", return_value=["code-review"]):
            ctx = build_system_context(task, skills=[])
        assert "PHASE: shipping" in ctx
        assert "call the Skill tool for EACH of these skills" in ctx
        assert "/t3:review" in ctx
        assert "/code-review" in ctx

    def test_skills_with_content(self) -> None:
        """Ensure skill content is included when skills resolve to files."""
        tmp_dir = Path(tempfile.mkdtemp())
        skill_file = tmp_dir / "my-skill" / "SKILL.md"
        skill_file.parent.mkdir()
        skill_file.write_text("# Loaded Skill", encoding="utf-8")

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        with patch("teatree.agents.prompt.DEFAULT_SKILLS_DIR", tmp_dir):
            ctx = build_system_context(task, skills=["my-skill"])
        assert "# Loaded Skills" in ctx
        assert "# Loaded Skill" in ctx

    def test_with_lifecycle_skill_scopes_loading(self) -> None:
        """When lifecycle_skill is set, only that skill + rules get full content."""
        tmp_dir = Path(tempfile.mkdtemp())
        for name in ("rules", "test", "ac-django"):
            d = tmp_dir / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"# {name} instructions", encoding="utf-8")

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="testing")

        with patch("teatree.agents.prompt.DEFAULT_SKILLS_DIR", tmp_dir):
            ctx = build_system_context(
                task,
                skills=["ac-django", "rules", "test"],
                lifecycle_skill="test",
            )

        assert "# test instructions" in ctx
        assert "# rules instructions" in ctx
        assert "# ac-django instructions" not in ctx
        assert "COMPANION SKILLS" in ctx

    def test_empty_skill_content(self) -> None:
        """When skills list is non-empty but no SKILL.md found, skip the section."""
        with patch("teatree.agents.prompt._read_skill_contents", return_value=""):
            ticket = Ticket.objects.create()
            session = Session.objects.create(ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session)

            ctx = build_system_context(task, skills=["nonexistent"])
            assert "# Loaded Skills" not in ctx

    def test_includes_parent_result(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        parent = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(
            task=parent,
            execution_target="headless",
            result={"summary": "Prior work done"},
        )
        child = Task.objects.create(ticket=ticket, session=session, parent_task=parent)

        ctx = build_system_context(child, skills=[])

        assert "Prior Task Result" in ctx
        assert "Prior work done" in ctx

    def test_includes_context_budget(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_system_context(task, skills=[])

        assert "Context Budget" in ctx
        assert "Truncate file reads" in ctx


# --- build_interactive_context ---


class TestBuildInteractiveContext(TestCase):
    def test_basic(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/99")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "interactive TeaTree session" in ctx
        assert "https://example.com/issues/99" in ctx
        assert "99" in ctx

    def test_with_title_and_phase(self) -> None:
        ticket = Ticket.objects.create(extra={"issue_title": "Implement feature X"})
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        ctx = build_interactive_context(task, skills=[])
        assert "Implement feature X" in ctx
        assert "Phase: coding" in ctx

    def test_with_reason_shows_diagnosis_prompt(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_reason="Agent needs guidance on API design",
        )

        ctx = build_interactive_context(task, skills=[])
        assert "Agent needs guidance on API design" in ctx
        assert "diagnosis" in ctx
        assert "Do NOT ask the user what happened" in ctx
        # Should NOT contain the generic acknowledgment prompt
        assert "acknowledge the project" not in ctx

    def test_without_reason_shows_acknowledgment_prompt(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "acknowledge the project" in ctx
        # Should NOT contain the diagnosis prompt
        assert "diagnosis" not in ctx

    def test_with_skills(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=["code", "test"])
        assert "/code" in ctx
        assert "/test" in ctx
        assert "REQUIRED" in ctx

    def test_with_prs(self) -> None:
        ticket = Ticket.objects.create(
            extra={
                "prs": {
                    "repo": {
                        "url": "https://gitlab.com/mr/5",
                        "title": "MR Title",
                        "draft": True,
                        "pipeline_status": "failed",
                    },
                },
            },
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "https://gitlab.com/mr/5" in ctx
        assert "(draft)" in ctx
        assert "pipeline: failed" in ctx
        assert "MR Title" in ctx

    def test_skips_non_dict_pr(self) -> None:
        ticket = Ticket.objects.create(
            extra={"prs": {"bad": 42, "ok": {"url": "https://x.com/mr/7"}}},
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "https://x.com/mr/7" in ctx

    def test_non_dict_prs(self) -> None:
        ticket = Ticket.objects.create(extra={"prs": "not-a-dict"})
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "pull requests" not in ctx.lower()

    def test_pr_no_title_no_pipeline(self) -> None:
        ticket = Ticket.objects.create(
            extra={"prs": {"repo": {"url": "https://x.com/mr/8"}}},
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "https://x.com/mr/8" in ctx
        assert "(draft)" not in ctx
        assert "pipeline:" not in ctx

    def test_non_dict_extra(self) -> None:
        ticket = Ticket.objects.create(extra="not-a-dict")
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "interactive TeaTree session" in ctx


# --- _parent_result_summary ---


class TestParentResultSummary(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket)

    def test_includes_prior_result(self) -> None:
        parent = Task.objects.create(ticket=self.ticket, session=self.session)
        TaskAttempt.objects.create(
            task=parent,
            execution_target="headless",
            result={
                "summary": "Implemented feature X",
                "files_modified": ["src/a.py", "src/b.py"],
                "next_steps": ["Run tests", "Deploy"],
            },
        )
        child = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=parent)

        summary = _parent_result_summary(child)

        assert "Implemented feature X" in summary
        assert "src/a.py" in summary
        assert "Run tests" in summary

    def test_empty_without_parent(self) -> None:
        task = Task.objects.create(ticket=self.ticket, session=self.session)

        assert _parent_result_summary(task) == ""

    def test_empty_without_attempts(self) -> None:
        parent = Task.objects.create(ticket=self.ticket, session=self.session)
        child = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=parent)

        assert _parent_result_summary(child) == ""

    def test_handles_non_dict_result(self) -> None:
        parent = Task.objects.create(ticket=self.ticket, session=self.session)
        TaskAttempt.objects.create(task=parent, execution_target="headless", result="not-a-dict")
        child = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=parent)

        assert _parent_result_summary(child) == ""


# --- build_reviewer_dispatch_prompt ---


class TestBuildReviewerDispatchPrompt(TestCase):
    """The shared reviewer dispatch-prompt builder embeds the overlay review skills.

    A review sub-agent dispatched via the Agent tool / a dynamic workflow /
    a headless reviewer structurally loads them through the REQUIRED load
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


# --- coding-phase builder dispatch contract (symmetric to reviewer prompt) ---


class TestCodingPhaseDispatchContract(TestCase):
    """Coding-phase builder prompt carries the dispatch-contract directive.

    The forced-load + behavior-preservation + no-AI-signature clauses are
    symmetric to ``build_reviewer_dispatch_prompt``.

    Pins the dispatch-contract symmetry: the reviewer path force-loads its
    skills, but the builder path historically only said "run tests before
    declaring done" — so the enumerate-and-preserve discipline never reached
    a dispatched builder. These assertions keep the contract from silently
    regressing.
    """

    def _coding_task(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(ticket=ticket, session=session, phase="coding")

    def test_task_prompt_has_forced_load_directive(self) -> None:
        prompt = build_task_prompt(self._coding_task())
        assert "/t3:architecture-design" in prompt
        assert "/t3:code" in prompt
        assert "REQUIRED: before writing code" in prompt

    def test_task_prompt_has_behavior_preservation_clause(self) -> None:
        prompt = build_task_prompt(self._coding_task())
        assert "BEHAVIOR PRESERVATION" in prompt
        assert "enumerate every behavior" in prompt
        assert "NEVER invert a must-block test to must-not-block" in prompt
        assert "weakening a" in prompt
        assert "privacy gate is a BLOCKER" in prompt

    def test_task_prompt_has_no_ai_signature_clause(self) -> None:
        prompt = build_task_prompt(self._coding_task())
        assert "NO AI SIGNATURE" in prompt
        assert "Co-Authored-By" in prompt
        assert "Generated with Claude Code" in prompt

    def test_task_prompt_has_open_questions_clause(self) -> None:
        prompt = build_task_prompt(self._coding_task())
        assert "OPEN QUESTIONS & ASSUMPTIONS" in prompt
        assert "Open questions & assumptions" in prompt
        assert "commit message" in prompt
        assert "PR description" in prompt

    def test_task_prompt_verify_step_replaces_bare_run_tests(self) -> None:
        prompt = build_task_prompt(self._coding_task())
        # Step 5 now points at the CI-parity verify command, not the vague
        # "Run tests before declaring done".
        assert "t3 tool verify-gates" in prompt
        assert "Run tests before declaring done" not in prompt

    def test_non_coding_task_prompt_has_no_coding_directive(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        prompt = build_task_prompt(task)
        assert "BEHAVIOR PRESERVATION" not in prompt
        assert "REQUIRED: before writing code" not in prompt

    def test_system_context_coding_phase_embeds_directive(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        ctx = build_system_context(task, skills=["code", "rules"], lifecycle_skill="code")
        assert "PHASE: coding" in ctx
        assert "/t3:architecture-design" in ctx
        assert "BEHAVIOR PRESERVATION" in ctx
        assert "NO AI SIGNATURE" in ctx
        assert "t3 tool verify-gates" in ctx

    def test_system_context_coding_phase_embeds_architecture_design_in_full(self) -> None:
        """architecture-design is a primary (full-embed) skill on the coding phase."""
        tmp_dir = Path(tempfile.mkdtemp())
        for name in ("rules", "code", "architecture-design"):
            d = tmp_dir / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"# {name} SENTINEL BODY", encoding="utf-8")
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        with patch("teatree.agents.prompt.DEFAULT_SKILLS_DIR", tmp_dir):
            ctx = build_system_context(
                task,
                skills=["code", "rules", "architecture-design"],
                lifecycle_skill="code",
            )
        # Full body, not the demoted "available — load if needed" summary.
        assert "# architecture-design SENTINEL BODY" in ctx
        assert "- architecture-design: available — load if needed" not in ctx


# --- #1368: explicit stack + overlay skill-load block on code-touching dispatch ---


class TestCodingPhaseStackSkillLoadInjection(TestCase):
    """A code-touching dispatch prompt force-loads the stack + overlay skills.

    #1368: a dispatched builder relies on auto-detect for ``/ac-django`` /
    ``/ac-python`` and the active overlay skill, which mis-fires when the
    worktree shape doesn't trip the detector (dispatched in /tmp, renamed
    SKILL.md, no parent-skill inheritance). The resolved bundle already carries
    them, so both builder prompts must inject them as an explicit "load BEFORE
    code" block — never rely on auto-detect.
    """

    def _coding_task(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(ticket=ticket, session=session, phase="coding")

    def test_task_prompt_django_stack_load_block(self) -> None:
        # The bundle reaching the builder is already requires-resolved, so a
        # Django repo carries both ac-django and ac-python (#1368).
        prompt = build_task_prompt(
            self._coding_task(),
            skills=["t3:demo-overlay", "ac-django", "ac-python", "code", "rules"],
        )
        assert "/ac-django" in prompt
        assert "/ac-python" in prompt
        assert "/t3:demo-overlay" in prompt
        assert "do NOT rely on auto-detect" in prompt

    def test_system_context_django_stack_load_block(self) -> None:
        ctx = build_system_context(
            self._coding_task(),
            skills=["t3:demo-overlay", "ac-django", "ac-python", "code", "rules"],
            lifecycle_skill="code",
        )
        assert "/ac-django" in ctx
        assert "/ac-python" in ctx
        assert "/t3:demo-overlay" in ctx

    def test_stack_block_does_not_relist_directive_forced_skills(self) -> None:
        prompt = build_task_prompt(self._coding_task(), skills=["ac-django", "code", "rules", "architecture-design"])
        stack_block = prompt.split("stack/overlay skills:")[1]
        # code / architecture-design / rules are force-loaded by the directive's
        # own lines, never re-listed in the stack block.
        assert "/code" not in stack_block
        assert "/architecture-design" not in stack_block
        assert "/rules" not in stack_block

    def test_unresolved_stack_emits_conservative_default(self) -> None:
        prompt = build_task_prompt(self._coding_task(), skills=["code", "rules"])
        assert "could not be auto-resolved" in prompt
        assert "/ac-django for a Django repo" in prompt
        assert "do NOT skip this" in prompt

    def test_no_skills_passed_still_emits_conservative_default(self) -> None:
        prompt = build_task_prompt(self._coding_task())
        assert "could not be auto-resolved" in prompt

    def test_stack_block_dedupes_bare_and_path_forms(self) -> None:
        prompt = build_task_prompt(
            self._coding_task(),
            skills=["ac-python", "some/path/ac-python/SKILL.md", "code", "rules"],
        )
        stack_block = prompt.split("stack/overlay skills:")[1]
        assert stack_block.count("/ac-python") == 1

    def test_non_coding_task_has_no_stack_load_block(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        prompt = build_task_prompt(task, skills=["ac-django", "t3:demo-overlay"])
        assert "stack/overlay skills" not in prompt
        assert "could not be auto-resolved" not in prompt

    def test_summary_does_not_contradict_directive(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp())
        for name in ("rules", "code", "architecture-design", "ac-django", "t3:demo-overlay"):
            d = tmp_dir / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"# {name} BODY", encoding="utf-8")
        with patch("teatree.agents.prompt.DEFAULT_SKILLS_DIR", tmp_dir):
            ctx = build_system_context(
                self._coding_task(),
                skills=["ac-django", "t3:demo-overlay", "code", "rules", "architecture-design"],
                lifecycle_skill="code",
            )
        # The force-loaded stack/overlay skills are NOT demoted to the ignorable
        # summary that would undercut the directive's "REQUIRED load" block.
        assert "- ac-django: available — load if needed" not in ctx
        assert "- t3:demo-overlay: available — load if needed" not in ctx
        assert "/ac-django" in ctx
