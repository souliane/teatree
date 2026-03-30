"""Build agent prompts from ticket and task context."""

from pathlib import Path

from teetree.core.models import Task, Ticket
from teetree.skill_loading import DEFAULT_SKILL_SEARCH_DIRS, find_skill_md

_ALWAYS_FULL_SKILLS = frozenset({"t3-rules"})


def _read_skill_contents(skills: list[str], *, skills_dir: Path | list[Path] = DEFAULT_SKILL_SEARCH_DIRS) -> str:
    """Read and concatenate SKILL.md content for each resolved skill."""
    sections: list[str] = []
    for name in skills:
        skill_md = find_skill_md(name, skills_dir)
        if skill_md is not None:
            content = skill_md.read_text(encoding="utf-8")
            sections.append(f"--- SKILL: {name} ---\n{content}")
    return "\n\n".join(sections)


def _is_primary(name: str, primary_skills: set[str]) -> bool:
    """Check if a skill name (or path) matches the primary set or always-full list."""
    if name in primary_skills or name in _ALWAYS_FULL_SKILLS:
        return True
    # Support absolute paths: extract the skill directory name
    skill_dir_name = Path(name).parent.name if "/" in name else ""
    return skill_dir_name in primary_skills or skill_dir_name in _ALWAYS_FULL_SKILLS


def _read_skill_contents_scoped(
    skills: list[str],
    *,
    primary_skills: set[str],
    skills_dir: Path | list[Path] = DEFAULT_SKILL_SEARCH_DIRS,
) -> str:
    """Read skills with scoping: primary skills get full content, others get a summary line."""
    sections: list[str] = []
    companion_names: list[str] = []
    for name in skills:
        if _is_primary(name, primary_skills):
            skill_md = find_skill_md(name, skills_dir)
            if skill_md is not None:
                content = skill_md.read_text(encoding="utf-8")
                sections.append(f"--- SKILL: {name} ---\n{content}")
        else:
            companion_names.append(name)
    if companion_names:
        summary = "--- COMPANION SKILLS (loaded but summarized to save context) ---\n"
        summary += "\n".join(f"- {name}: available — load if needed" for name in companion_names)
        sections.append(summary)
    return "\n\n".join(sections)


_MAX_PARENT_SUMMARY_LEN = 2000


def _parent_result_summary(task: Task) -> str:
    """Return a compact summary from the parent task's last attempt result."""
    parent = task.parent_task
    if parent is None:
        return ""
    last_attempt = parent.attempts.order_by("-pk").first()
    if last_attempt is None:
        return ""
    result = last_attempt.result if isinstance(last_attempt.result, dict) else {}
    parts: list[str] = []
    if summary := str(result.get("summary", "")):
        parts.append(f"Summary: {summary[:_MAX_PARENT_SUMMARY_LEN]}")
    if files := result.get("files_modified"):
        parts.append(f"Files modified: {', '.join(str(f) for f in files[:20])}")
    if steps := result.get("next_steps"):
        parts.append(f"Next steps: {', '.join(str(s) for s in steps[:10])}")
    return "\n".join(parts)


def build_task_prompt(task: Task) -> str:
    """Build a work prompt for a headless agent."""
    ticket: Ticket = task.ticket
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}

    lines = [f"Work on ticket {ticket.ticket_number}."]

    if ticket.issue_url:
        lines.append(f"Issue: {ticket.issue_url}")

    if title := extra.get("issue_title"):
        lines.append(f"Title: {title}")

    if labels := extra.get("labels"):
        lines.append(f"Labels: {', '.join(labels)}")

    if task.phase:
        lines.append(f"Current phase: {task.phase}")

    if task.execution_reason:
        lines.append(f"Reason: {task.execution_reason}")

    # MR context
    mrs = extra.get("mrs", {})
    if isinstance(mrs, dict) and mrs:
        lines.extend(("", "Open merge requests:"))
        for mr in mrs.values():
            if not isinstance(mr, dict):
                continue
            url = mr.get("url", "")
            title = mr.get("title", "")
            draft = " (draft)" if mr.get("draft") else ""
            pipeline = mr.get("pipeline_status", "")
            pipeline_info = f" — pipeline: {pipeline}" if pipeline else ""
            lines.append(f"  - {url}{draft}{pipeline_info}")
            if title:
                lines.append(f"    {title}")

    lines.extend(
        (
            "",
            "Instructions:",
            "1. Check what has been done so far (git log, existing code, MR status)",
            "2. Identify what remains to be done",
            "3. If you can proceed (code, test, fix) — do it",
            "4. If you need human input (design decision, access, clarification) — say so clearly",
            "5. Run tests before declaring done",
        )
    )

    return "\n".join(lines)


def build_system_context(task: Task, *, skills: list[str], lifecycle_skill: str = "") -> str:
    """Build the system context for headless (SDK) execution.

    When *lifecycle_skill* is provided, only the lifecycle skill and t3-rules
    are embedded in full; companion skills get a one-line summary to save tokens.
    """
    lines = ["You are a TeaTree headless agent executing a task."]
    lines.extend((f"Task ID: {task.pk}", f"Ticket: {task.ticket.ticket_number}"))

    # Context bridge: include parent task result so follow-up tasks
    # don't need full session resume to understand prior work.
    parent_summary = _parent_result_summary(task)
    if parent_summary:
        lines.extend(("", "# Prior Task Result", "", parent_summary))

    if skills:
        if lifecycle_skill:
            skill_content = _read_skill_contents_scoped(skills, primary_skills={lifecycle_skill})
        else:
            skill_content = _read_skill_contents(skills)
        if skill_content:
            lines.extend(("", "# Loaded Skills", "", skill_content))

    if task.phase == "reviewing":
        lines.extend(
            (
                "",
                "PHASE: reviewing",
                "1. Do a thorough code review of all changes on this ticket's branch.",
                "2. Run /t3-next when done — it handles retro + structured result + handoff.",
            )
        )

    lines.extend(
        (
            "",
            "# Context Budget",
            "- Truncate file reads to the relevant section — avoid reading entire large files.",
            "- Limit git diff output to 200 lines; use --stat for overview first.",
            "- Summarize test output instead of pasting full logs.",
            "",
            "When done, run /t3-next to wrap up. It will:",
            "- Run /t3-retro (captures lessons while context is fresh)",
            "- Emit the structured JSON result the pipeline needs",
            "- Display a summary of what happened",
            "",
            "If /t3-next is not available, output a JSON object on the last line:",
            '  {"summary": "...", "needs_user_input": false, "files_modified": [...], "next_steps": [...]}',
        )
    )

    return "\n".join(lines)


def build_interactive_context(task: Task, *, skills: list[str]) -> str:
    """Build the system context for interactive (ttyd) sessions."""
    ticket: Ticket = task.ticket
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}

    lines = ["You are working in an interactive TeaTree session."]
    lines.extend((f"Task ID: {task.pk}", f"Ticket: {ticket.ticket_number}"))

    if ticket.issue_url:
        lines.append(f"Issue: {ticket.issue_url}")

    if title := extra.get("issue_title"):
        lines.append(f"Title: {title}")

    if task.phase:
        lines.append(f"Phase: {task.phase}")

    if task.execution_reason:
        lines.extend(("", f"What to do: {task.execution_reason}"))

    if skills:
        lines.extend(
            (
                "",
                "REQUIRED: Before starting any work, call the Skill tool for EACH of these skills:",
                *(f"  - /{skill}" for skill in skills),
                "Do this FIRST, before reading files, running commands, or responding to the user.",
            )
        )

    # MR context
    mrs = extra.get("mrs", {})
    if isinstance(mrs, dict) and mrs:
        lines.extend(("", "Open merge requests:"))
        for mr in mrs.values():
            if not isinstance(mr, dict):
                continue
            url = mr.get("url", "")
            mr_title = mr.get("title", "")
            draft = " (draft)" if mr.get("draft") else ""
            pipeline = mr.get("pipeline_status", "")
            pipeline_info = f" — pipeline: {pipeline}" if pipeline else ""
            lines.append(f"  - {url}{draft}{pipeline_info}")
            if mr_title:
                lines.append(f"    {mr_title}")

    lines.extend(
        (
            "",
            "This is an interactive session — the user is present.",
            "Your FIRST message must acknowledge the project and ticket you are working on.",
            "Summarize: ticket number, current state, what was done so far, and what you plan to do next.",
            "Then either begin working or ask the user for guidance.",
            "Before ending, run /t3-next — it handles retro, result reporting, and pipeline handoff.",
        )
    )

    return "\n".join(lines)
