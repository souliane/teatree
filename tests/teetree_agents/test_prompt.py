"""Tests for teetree.agents.prompt — agent prompt building."""

from pathlib import Path

import pytest

from teetree.agents.prompt import (
    _is_primary,
    _read_skill_contents,
    _read_skill_contents_scoped,
    build_interactive_context,
    build_system_context,
    build_task_prompt,
)
from teetree.core.models import Session, Task, Ticket

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
    assert _is_primary("t3-test", {"t3-test"})
    assert not _is_primary("t3-code", {"t3-test"})


def test_is_primary_matches_always_full() -> None:
    assert _is_primary("t3-rules", set())


def test_is_primary_matches_absolute_path() -> None:
    assert _is_primary("/tmp/skills/t3-test/SKILL.md", {"t3-test"})
    assert _is_primary("/tmp/skills/t3-rules/SKILL.md", set())
    assert not _is_primary("/tmp/skills/ac-django/SKILL.md", {"t3-test"})


# --- _read_skill_contents_scoped ---


def test_read_scoped_embeds_primary_and_summarizes_companions(tmp_path: Path) -> None:
    for name in ("t3-rules", "t3-test", "ac-django", "t3-workspace"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name} full content", encoding="utf-8")

    result = _read_skill_contents_scoped(
        ["ac-django", "t3-workspace", "t3-rules", "t3-test"],
        primary_skills={"t3-test"},
        skills_dir=tmp_path,
    )
    # Primary skills get full content
    assert "--- SKILL: t3-test ---" in result
    assert "# t3-test full content" in result
    # t3-rules is always fully loaded
    assert "--- SKILL: t3-rules ---" in result
    assert "# t3-rules full content" in result
    # Companion skills get summary only
    assert "COMPANION SKILLS" in result
    assert "- ac-django:" in result
    assert "- t3-workspace:" in result
    assert "# ac-django full content" not in result
    assert "# t3-workspace full content" not in result


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


@pytest.mark.django_db
def test_build_task_prompt_basic() -> None:
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/42")
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    prompt = build_task_prompt(task)
    assert "42" in prompt
    assert "https://example.com/issues/42" in prompt


@pytest.mark.django_db
def test_build_task_prompt_includes_title_and_labels() -> None:
    ticket = Ticket.objects.create(
        issue_url="https://example.com/issues/1",
        extra={"issue_title": "Fix the bug", "labels": ["bug", "urgent"]},
    )
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    prompt = build_task_prompt(task)
    assert "Fix the bug" in prompt
    assert "bug, urgent" in prompt


@pytest.mark.django_db
def test_build_task_prompt_includes_phase_and_reason() -> None:
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


@pytest.mark.django_db
def test_build_task_prompt_includes_mr_context() -> None:
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


@pytest.mark.django_db
def test_build_task_prompt_skips_non_dict_mr_items() -> None:
    ticket = Ticket.objects.create(
        extra={"mrs": {"bad": "not-a-dict", "good": {"url": "https://x.com/mr/2"}}},
    )
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    prompt = build_task_prompt(task)
    assert "https://x.com/mr/2" in prompt


@pytest.mark.django_db
def test_build_task_prompt_handles_non_dict_extra() -> None:
    ticket = Ticket.objects.create(extra="not-a-dict")
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    prompt = build_task_prompt(task)
    assert "Work on ticket" in prompt


@pytest.mark.django_db
def test_build_task_prompt_mr_without_title_or_pipeline() -> None:
    ticket = Ticket.objects.create(
        extra={"mrs": {"repo": {"url": "https://x.com/mr/3", "draft": False}}},
    )
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    prompt = build_task_prompt(task)
    assert "https://x.com/mr/3" in prompt
    assert "(draft)" not in prompt
    assert "pipeline:" not in prompt


@pytest.mark.django_db
def test_build_task_prompt_non_dict_mrs_ignored() -> None:
    ticket = Ticket.objects.create(extra={"mrs": "not-a-dict"})
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    prompt = build_task_prompt(task)
    assert "merge requests" not in prompt.lower()


# --- build_system_context ---


@pytest.mark.django_db
def test_build_system_context_basic() -> None:
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/10")
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_system_context(task, skills=[])
    assert "TeaTree headless agent" in ctx
    assert "10" in ctx
    assert "/t3-next" in ctx


@pytest.mark.django_db
def test_build_system_context_with_skills(tmp_path: Path) -> None:
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Test Skill Content", encoding="utf-8")

    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_system_context(task, skills=["test-skill"])
    # skill content is read from default skills_dir, not tmp_path — so skill will not be found.
    # The test verifies the code path is exercised (lines 78-81).
    assert "TeaTree headless agent" in ctx


@pytest.mark.django_db
def test_build_system_context_reviewing_phase() -> None:
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")

    ctx = build_system_context(task, skills=[])
    assert "PHASE: reviewing" in ctx
    assert "code review" in ctx


@pytest.mark.django_db
def test_build_system_context_skills_with_content(tmp_path: Path) -> None:
    """Ensure skill content is included when skills resolve to files."""
    skill_file = tmp_path / "my-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# Loaded Skill", encoding="utf-8")

    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    # Use absolute file path so find_skill_md resolves it directly
    ctx = build_system_context(task, skills=[str(skill_file)])
    assert "# Loaded Skills" in ctx
    assert "# Loaded Skill" in ctx


@pytest.mark.django_db
def test_build_system_context_with_lifecycle_skill_scopes_loading(tmp_path: Path) -> None:
    """When lifecycle_skill is set, only that skill + t3-rules get full content."""
    for name in ("t3-rules", "t3-test", "ac-django"):
        d = tmp_path / name
        d.mkdir()
        (d / "SKILL.md").write_text(f"# {name} instructions", encoding="utf-8")

    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, phase="testing")

    # Use absolute SKILL.md paths so find_skill_md resolves them directly
    skills = [str(tmp_path / n / "SKILL.md") for n in ("ac-django", "t3-rules", "t3-test")]
    lifecycle = str(tmp_path / "t3-test" / "SKILL.md")
    ctx = build_system_context(task, skills=skills, lifecycle_skill=lifecycle)

    assert "# t3-test instructions" in ctx
    assert "# t3-rules instructions" in ctx
    assert "# ac-django instructions" not in ctx
    assert "COMPANION SKILLS" in ctx


@pytest.mark.django_db
def test_build_system_context_empty_skill_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """When skills list is non-empty but no SKILL.md found, skip the section."""
    monkeypatch.setattr("teetree.agents.prompt._read_skill_contents", lambda *_a, **_kw: "")

    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_system_context(task, skills=["nonexistent"])
    assert "# Loaded Skills" not in ctx


# --- build_interactive_context ---


@pytest.mark.django_db
def test_build_interactive_context_basic() -> None:
    ticket = Ticket.objects.create(issue_url="https://example.com/issues/99")
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_interactive_context(task, skills=[])
    assert "interactive TeaTree session" in ctx
    assert "https://example.com/issues/99" in ctx
    assert "99" in ctx


@pytest.mark.django_db
def test_build_interactive_context_with_title_and_phase() -> None:
    ticket = Ticket.objects.create(extra={"issue_title": "Implement feature X"})
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session, phase="coding")

    ctx = build_interactive_context(task, skills=[])
    assert "Implement feature X" in ctx
    assert "Phase: coding" in ctx


@pytest.mark.django_db
def test_build_interactive_context_with_reason() -> None:
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(
        ticket=ticket,
        session=session,
        execution_reason="Agent needs guidance on API design",
    )

    ctx = build_interactive_context(task, skills=[])
    assert "Agent needs guidance on API design" in ctx


@pytest.mark.django_db
def test_build_interactive_context_with_skills() -> None:
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_interactive_context(task, skills=["t3-code", "t3-test"])
    assert "/t3-code" in ctx
    assert "/t3-test" in ctx
    assert "REQUIRED" in ctx


@pytest.mark.django_db
def test_build_interactive_context_with_mrs() -> None:
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


@pytest.mark.django_db
def test_build_interactive_context_skips_non_dict_mr() -> None:
    ticket = Ticket.objects.create(
        extra={"mrs": {"bad": 42, "ok": {"url": "https://x.com/mr/7"}}},
    )
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_interactive_context(task, skills=[])
    assert "https://x.com/mr/7" in ctx


@pytest.mark.django_db
def test_build_interactive_context_non_dict_mrs() -> None:
    ticket = Ticket.objects.create(extra={"mrs": "not-a-dict"})
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_interactive_context(task, skills=[])
    assert "merge requests" not in ctx.lower()


@pytest.mark.django_db
def test_build_interactive_context_mr_no_title_no_pipeline() -> None:
    ticket = Ticket.objects.create(
        extra={"mrs": {"repo": {"url": "https://x.com/mr/8"}}},
    )
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_interactive_context(task, skills=[])
    assert "https://x.com/mr/8" in ctx
    assert "(draft)" not in ctx
    assert "pipeline:" not in ctx


@pytest.mark.django_db
def test_build_interactive_context_non_dict_extra() -> None:
    ticket = Ticket.objects.create(extra="not-a-dict")
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(ticket=ticket, session=session)

    ctx = build_interactive_context(task, skills=[])
    assert "interactive TeaTree session" in ctx
