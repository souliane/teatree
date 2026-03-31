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

    def test_includes_mr_context(self) -> None:
        ticket = Ticket.objects.create(
            extra={
                "mrs": {
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

    def test_skips_non_dict_mr_items(self) -> None:
        ticket = Ticket.objects.create(
            extra={"mrs": {"bad": "not-a-dict", "good": {"url": "https://x.com/mr/2"}}},
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

    def test_mr_without_title_or_pipeline(self) -> None:
        ticket = Ticket.objects.create(
            extra={"mrs": {"repo": {"url": "https://x.com/mr/3", "draft": False}}},
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "https://x.com/mr/3" in prompt
        assert "(draft)" not in prompt
        assert "pipeline:" not in prompt

    def test_non_dict_mrs_ignored(self) -> None:
        ticket = Ticket.objects.create(extra={"mrs": "not-a-dict"})
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        prompt = build_task_prompt(task)
        assert "merge requests" not in prompt.lower()


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

    def test_with_reason(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_reason="Agent needs guidance on API design",
        )

        ctx = build_interactive_context(task, skills=[])
        assert "Agent needs guidance on API design" in ctx

    def test_with_skills(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=["code", "test"])
        assert "/code" in ctx
        assert "/test" in ctx
        assert "REQUIRED" in ctx

    def test_with_mrs(self) -> None:
        ticket = Ticket.objects.create(
            extra={
                "mrs": {
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

    def test_skips_non_dict_mr(self) -> None:
        ticket = Ticket.objects.create(
            extra={"mrs": {"bad": 42, "ok": {"url": "https://x.com/mr/7"}}},
        )
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "https://x.com/mr/7" in ctx

    def test_non_dict_mrs(self) -> None:
        ticket = Ticket.objects.create(extra={"mrs": "not-a-dict"})
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)

        ctx = build_interactive_context(task, skills=[])
        assert "merge requests" not in ctx.lower()

    def test_mr_no_title_no_pipeline(self) -> None:
        ticket = Ticket.objects.create(
            extra={"mrs": {"repo": {"url": "https://x.com/mr/8"}}},
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
