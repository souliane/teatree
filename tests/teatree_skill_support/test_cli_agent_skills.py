"""Tests for ``t3 agent`` skill selection policy.

Selection is explicit: a phase, explicit skills, or a ticket status — never a
free-text scan of a task description. A launch with none of those asks the user.
"""

from pathlib import Path

from teatree.skill_support.loading import SkillLoadingPolicy


def _launch(tmp_path: Path, **overrides):
    policy = SkillLoadingPolicy()
    defaults = {
        "cwd": tmp_path,
        "overlay_skill_metadata": {},
        "ticket_status": "",
        "explicit_phase": "",
        "explicit_skills": [],
        "overlay_active": False,
    }
    defaults.update(overrides)
    return policy.select_for_agent_launch(**defaults)


def test_agent_without_status_phase_or_skill_asks_user(tmp_path: Path) -> None:
    result = _launch(tmp_path)
    assert result.skills == []
    assert result.ask_user is True


def test_agent_explicit_phase_selects_debug_skill(tmp_path: Path) -> None:
    result = _launch(tmp_path, explicit_phase="debugging")
    assert result.skills == ["debug"]
    assert result.lifecycle_skill == "debug"
    assert result.ask_user is False


def test_agent_explicit_phase_selects_review_skill(tmp_path: Path) -> None:
    result = _launch(tmp_path, explicit_phase="reviewing")
    assert result.skills == ["review"]
    assert result.lifecycle_skill == "review"


def test_agent_explicit_skills_preserve_order(tmp_path: Path) -> None:
    result = _launch(tmp_path, explicit_skills=["test", "review"])
    assert result.skills == ["test", "review"]


def test_agent_overlay_skill_comes_from_metadata_not_local_skill_scan(tmp_path: Path) -> None:
    result = _launch(
        tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme"},
        explicit_phase="reviewing",
        overlay_active=True,
    )
    assert result.skills == ["t3-acme", "review"]


def test_agent_ticket_status_selects_skill(tmp_path: Path) -> None:
    result = _launch(tmp_path, ticket_status="reviewed")
    assert result.skills == ["ship"]
    assert result.lifecycle_skill == "ship"
