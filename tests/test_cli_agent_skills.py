"""Tests for ``t3 agent`` skill selection policy."""

from pathlib import Path

from teatree.skill_loading import SkillLoadingPolicy


def test_agent_without_status_or_task_asks_user(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()

    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="",
        ticket_status="",
        explicit_phase="",
        explicit_skills=[],
        overlay_active=False,
    )

    assert result.skills == []
    assert result.ask_user is True


def test_agent_task_text_selects_debug_skill(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()

    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="fix the sync bug",
        ticket_status="",
        explicit_phase="",
        explicit_skills=[],
        overlay_active=False,
    )

    assert result.skills == ["debug"]
    assert result.lifecycle_skill == "debug"
    assert result.ask_user is False


def test_agent_task_text_selects_review_skill(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()

    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="review the code",
        ticket_status="",
        explicit_phase="",
        explicit_skills=[],
        overlay_active=False,
    )

    assert result.skills == ["review"]
    assert result.lifecycle_skill == "review"


def test_agent_explicit_skills_preserve_order(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()

    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="",
        ticket_status="",
        explicit_phase="",
        explicit_skills=["test", "review"],
        overlay_active=False,
    )

    assert result.skills == ["test", "review"]


def test_agent_overlay_skill_comes_from_metadata_not_local_skill_scan(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()

    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={"skill_path": "t3-acme"},
        task="review the code",
        ticket_status="",
        explicit_phase="",
        explicit_skills=[],
        overlay_active=True,
    )

    assert result.skills == ["t3-acme", "review"]


def test_agent_status_beats_task_text(tmp_path: Path) -> None:
    policy = SkillLoadingPolicy()

    result = policy.select_for_agent_launch(
        cwd=tmp_path,
        overlay_skill_metadata={},
        task="fix the sync bug",
        ticket_status="reviewed",
        explicit_phase="",
        explicit_skills=[],
        overlay_active=False,
    )

    assert result.skills == ["ship"]
    assert result.lifecycle_skill == "ship"
