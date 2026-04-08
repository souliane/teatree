"""Tests for skill tracking in the hook router (handle_track_skill_usage)."""

from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import handle_track_skill_usage


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    """Point STATE_DIR at a temp directory so tests don't pollute /tmp."""
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


def _read_skills(session_id: str) -> list[str]:
    skills_file = router.STATE_DIR / f"{session_id}.skills"
    if not skills_file.is_file():
        return []
    return [line for line in skills_file.read_text(encoding="utf-8").strip().splitlines() if line]


class TestPostToolUseSkillTracking:
    """Track skills from PostToolUse events (Skill tool calls)."""

    def test_tracks_skill_from_tool_input(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-1",
                "tool_name": "Skill",
                "tool_input": {"skill": "t3:code"},
            }
        )
        assert _read_skills("sess-1") == ["t3:code"]

    def test_deduplicates_skill_names(self) -> None:
        for _ in range(3):
            handle_track_skill_usage(
                {
                    "session_id": "sess-2",
                    "tool_name": "Skill",
                    "tool_input": {"skill": "t3:debug"},
                }
            )
        assert _read_skills("sess-2") == ["t3:debug"]

    def test_tracks_multiple_skills(self) -> None:
        for skill in ("t3:code", "t3:debug", "t3:test"):
            handle_track_skill_usage(
                {
                    "session_id": "sess-3",
                    "tool_name": "Skill",
                    "tool_input": {"skill": skill},
                }
            )
        assert _read_skills("sess-3") == ["t3:code", "t3:debug", "t3:test"]

    def test_ignores_missing_session_id(self) -> None:
        handle_track_skill_usage({"tool_input": {"skill": "code"}})
        # No file should be created for empty session
        assert not list(router.STATE_DIR.glob("*.skills"))

    def test_ignores_empty_skill_name(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-4",
                "tool_name": "Skill",
                "tool_input": {"skill": ""},
            }
        )
        assert _read_skills("sess-4") == []


class TestInstructionsLoadedSkillTracking:
    """Track skills from InstructionsLoaded events."""

    def test_tracks_skills_from_dict_objects(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-10",
                "skills": [{"name": "t3:code"}, {"name": "t3:debug"}],
            }
        )
        assert _read_skills("sess-10") == ["t3:code", "t3:debug"]

    def test_tracks_skills_from_string_names(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-11",
                "skills": ["t3:code", "t3:debug"],
            }
        )
        assert _read_skills("sess-11") == ["t3:code", "t3:debug"]

    def test_tracks_mixed_dicts_and_strings(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-12",
                "skills": [{"name": "t3:code"}, "t3:debug"],
            }
        )
        assert _read_skills("sess-12") == ["t3:code", "t3:debug"]

    def test_deduplicates_across_events(self) -> None:
        for _ in range(2):
            handle_track_skill_usage(
                {
                    "session_id": "sess-13",
                    "skills": [{"name": "t3:code"}],
                }
            )
        assert _read_skills("sess-13") == ["t3:code"]

    def test_ignores_empty_names_in_dicts(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-14",
                "skills": [{"name": ""}, {"name": "t3:code"}],
            }
        )
        assert _read_skills("sess-14") == ["t3:code"]

    def test_ignores_non_dict_non_string_items(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-15",
                "skills": [42, None, {"name": "t3:code"}],
            }
        )
        assert _read_skills("sess-15") == ["t3:code"]


class TestPostToolUsePrecedence:
    """PostToolUse skill tracking takes precedence (returns early)."""

    def test_tool_input_skill_takes_precedence_over_skills_array(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-20",
                "tool_input": {"skill": "t3:code"},
                "skills": [{"name": "t3:debug"}],
            }
        )
        # Only the PostToolUse path fires — t3:debug from InstructionsLoaded is NOT tracked
        assert _read_skills("sess-20") == ["t3:code"]
