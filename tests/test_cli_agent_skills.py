"""Tests for ``t3 agent`` dynamic skill resolution."""

from pathlib import Path

from teetree.cli import _resolve_agent_skills


def test_no_overlay_no_task_returns_defaults(tmp_path: Path) -> None:
    """Without overlay skills or task, returns default t3-code + t3-debug."""
    result = _resolve_agent_skills("", tmp_path)
    assert result == ["t3-code", "t3-debug"]


def test_overlay_skills_discovered_from_project(tmp_path: Path) -> None:
    """Overlay skills are discovered from the project's skills/ directory."""
    (tmp_path / "skills" / "t3-acme" / "SKILL.md").parent.mkdir(parents=True)
    (tmp_path / "skills" / "t3-acme" / "SKILL.md").touch()
    result = _resolve_agent_skills("", tmp_path)
    assert result[0] == "t3-acme"
    assert "t3-code" in result
    assert "t3-debug" in result


def test_overlay_skills_sorted_alphabetically(tmp_path: Path) -> None:
    """Multiple overlay skills are returned in sorted order."""
    for name in ("t3-zeta", "t3-alpha"):
        (tmp_path / "skills" / name).mkdir(parents=True)
        (tmp_path / "skills" / name / "SKILL.md").touch()
    result = _resolve_agent_skills("", tmp_path)
    assert result[0] == "t3-alpha"
    assert result[1] == "t3-zeta"


def test_task_keyword_debug_selects_debug_skill(tmp_path: Path) -> None:
    """Task mentioning 'fix' selects t3-debug."""
    result = _resolve_agent_skills("fix the sync bug", tmp_path)
    assert "t3-debug" in result


def test_task_keyword_test_selects_test_skill(tmp_path: Path) -> None:
    """Task mentioning 'pytest' selects t3-test."""
    result = _resolve_agent_skills("run pytest on the models", tmp_path)
    assert "t3-test" in result


def test_task_keyword_ship_selects_ship_skill(tmp_path: Path) -> None:
    """Task mentioning 'commit' selects t3-ship."""
    result = _resolve_agent_skills("commit and push", tmp_path)
    assert "t3-ship" in result


def test_task_keyword_review_selects_review_skill(tmp_path: Path) -> None:
    """Task mentioning 'review' selects t3-review."""
    result = _resolve_agent_skills("review the code", tmp_path)
    assert "t3-review" in result


def test_task_keyword_ticket_selects_ticket_skill(tmp_path: Path) -> None:
    """Task mentioning 'ticket' selects t3-ticket."""
    result = _resolve_agent_skills("start working on ticket 1234", tmp_path)
    assert "t3-ticket" in result


def test_task_keywords_case_insensitive(tmp_path: Path) -> None:
    """Keyword matching is case-insensitive."""
    result = _resolve_agent_skills("DEBUG the crash", tmp_path)
    assert "t3-debug" in result


def test_multiple_keywords_match_multiple_skills(tmp_path: Path) -> None:
    """A task matching multiple phases includes all matched skills."""
    result = _resolve_agent_skills("fix the bug and run tests", tmp_path)
    assert "t3-debug" in result
    assert "t3-test" in result


def test_overlay_skill_not_duplicated_when_also_lifecycle(tmp_path: Path) -> None:
    """If overlay skills dir contains a lifecycle skill name, don't duplicate it."""
    (tmp_path / "skills" / "t3-debug").mkdir(parents=True)
    (tmp_path / "skills" / "t3-debug" / "SKILL.md").touch()
    result = _resolve_agent_skills("", tmp_path)
    assert result.count("t3-debug") == 1


def test_no_skills_dir_returns_defaults(tmp_path: Path) -> None:
    """If the project has no skills/ directory, returns defaults."""
    result = _resolve_agent_skills("", tmp_path)
    assert result == ["t3-code", "t3-debug"]
