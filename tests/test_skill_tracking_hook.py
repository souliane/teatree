"""Tests for skill tracking in the hook router.

The session-end backstop itself lives in ``tests/test_session_end_work_check.py``,
mirroring its ``hooks/scripts/session_end_work_check.py`` module.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
import hooks.scripts.session_end_work_check as work_check
from hooks.scripts.hook_router import handle_track_skill_usage


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    """Point STATE_DIR at a temp directory so tests don't pollute /tmp."""
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


@pytest.fixture(autouse=True)
def _no_real_orphan_fetch():
    """Prevent session-end tests from shelling out to the real t3 CLI."""
    with patch.object(work_check, "fetch_orphans", return_value=[]):
        yield


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


def _write_skill(skills_dir: Path, name: str, *, requires: list[str] | None = None) -> None:
    """Write a real SKILL.md fixture with frontmatter the resolver parses."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"description: Fixture skill {name}.",
    ]
    if requires:
        lines.append("requires:")
        lines.extend(f"  - {dep}" for dep in requires)
    lines += [
        "triggers:",
        "  priority: 50",
        "  keywords:",
        f"    - '\\b{name}\\b'",
        "---",
        "",
        f"# {name}",
        "",
        f"Body of {name}.",
    ]
    (skill_dir / "SKILL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def skill_fixture_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a real skill tree and point the closure resolver at it.

    No mocking of the dependency resolver: ``build_requires_index`` parses
    these real SKILL.md files and ``resolve_requires`` walks them.
    """
    skills_dir = tmp_path / "skills"
    # rules has no deps; workspace requires rules; code requires workspace;
    # review requires code (which transitively pulls workspace + rules).
    _write_skill(skills_dir, "rules")
    _write_skill(skills_dir, "workspace", requires=["rules"])
    _write_skill(skills_dir, "code", requires=["workspace"])
    _write_skill(skills_dir, "review", requires=["code"])
    _write_skill(skills_dir, "debug", requires=["rules"])
    monkeypatch.setenv("T3_SKILL_SEARCH_DIRS", str(skills_dir))


@pytest.mark.usefixtures("skill_fixture_tree")
class TestRequiresClosureTracking:
    """Loading a skill records its resolved ``requires:`` closure (#689).

    The persisted ``.skills`` set is the fully-qualified canonical form: the
    WRITE boundary normalizes each closure member UP to its plugin namespace
    (:func:`normalize_skill_name`), so a plugin-owned ``code`` is recorded
    as ``t3:code`` regardless of whether the source event was the Skill tool
    (already namespaced) or InstructionsLoaded (bare). The fixture skills are
    plugin-owned (seeded under the ``T3_SKILL_SEARCH_DIRS`` override), so
    they canonicalize to ``t3:*``; ``ac-django`` is not owned and stays bare.
    """

    def test_post_tool_use_expands_to_requires_closure(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-clo-1",
                "tool_name": "Skill",
                "tool_input": {"skill": "code"},
            }
        )
        # code requires workspace, workspace requires rules — all must appear,
        # deps before dependents (topological order), as canonical names.
        assert _read_skills("sess-clo-1") == ["t3:rules", "t3:workspace", "t3:code"]

    def test_instructions_loaded_expands_to_requires_closure(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-clo-2",
                "skills": [{"name": "review"}],
            }
        )
        # review -> code -> workspace -> rules
        assert _read_skills("sess-clo-2") == ["t3:rules", "t3:workspace", "t3:code", "t3:review"]

    def test_closure_deduplicates_across_events(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-clo-3",
                "tool_name": "Skill",
                "tool_input": {"skill": "code"},
            }
        )
        handle_track_skill_usage(
            {
                "session_id": "sess-clo-3",
                "tool_name": "Skill",
                "tool_input": {"skill": "debug"},
            }
        )
        # rules/workspace/code from the first call; debug adds only itself
        # (rules already present) — no duplicates.
        assert _read_skills("sess-clo-3") == ["t3:rules", "t3:workspace", "t3:code", "t3:debug"]

    def test_string_names_in_instructions_loaded_get_closure(self) -> None:
        handle_track_skill_usage(
            {
                "session_id": "sess-clo-4",
                "skills": ["code"],
            }
        )
        assert _read_skills("sess-clo-4") == ["t3:rules", "t3:workspace", "t3:code"]

    def test_bare_then_namespaced_same_skill_recorded_once(self) -> None:
        # Legacy + Skill-tool spellings of the SAME skill must collapse: a
        # bare InstructionsLoaded ``debug`` then a namespaced Skill-tool
        # ``t3:debug`` record ``t3:debug`` once, not twice.
        handle_track_skill_usage({"session_id": "sess-clo-mixed", "skills": ["debug"]})
        handle_track_skill_usage(
            {"session_id": "sess-clo-mixed", "tool_name": "Skill", "tool_input": {"skill": "t3:debug"}}
        )
        assert _read_skills("sess-clo-mixed") == ["t3:rules", "t3:debug"]

    def test_unknown_skill_passes_through_without_error(self) -> None:
        # A framework skill with no trigger entry (ac-django) must still be
        # recorded — it just has no closure to expand and no plugin namespace.
        handle_track_skill_usage(
            {
                "session_id": "sess-clo-5",
                "tool_name": "Skill",
                "tool_input": {"skill": "ac-django"},
            }
        )
        assert _read_skills("sess-clo-5") == ["ac-django"]

    def test_suggested_but_not_loaded_skills_are_not_tracked(self) -> None:
        # Only genuinely-loaded skills + their closure are tracked. A skill
        # that was merely suggested (never the subject of a Skill tool call
        # or an InstructionsLoaded entry) must not appear.
        handle_track_skill_usage(
            {
                "session_id": "sess-clo-6",
                "tool_name": "Skill",
                "tool_input": {"skill": "debug"},
            }
        )
        tracked = _read_skills("sess-clo-6")
        assert tracked == ["t3:rules", "t3:debug"]
        # "code"/"workspace"/"review" were never loaded — suggested != loaded.
        assert "t3:code" not in tracked
        assert "t3:review" not in tracked
