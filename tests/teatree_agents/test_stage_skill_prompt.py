"""Tests for teatree.agents.stage_skill_prompt — per-stage skill prompt scoping."""

from types import SimpleNamespace
from unittest.mock import patch

from teatree.agents.stage_skill_prompt import stage_precedence_line, stage_skills_present


def _task(phase: str) -> SimpleNamespace:
    return SimpleNamespace(phase=phase)


def test_stage_skills_present_intersects_configured_with_bundle() -> None:
    with patch("teatree.agents.skill_bundle.active_overlay_stage_skills", return_value=["backend-dev", "absent"]):
        present = stage_skills_present(_task("coding"), ["rules", "backend-dev", "code"])
    assert present == ["backend-dev"]


def test_stage_skills_present_empty_when_none_configured() -> None:
    with patch("teatree.agents.skill_bundle.active_overlay_stage_skills", return_value=[]):
        assert stage_skills_present(_task("coding"), ["rules", "code"]) == []


def test_stage_skills_present_uses_threaded_configured_without_reresolving() -> None:
    # A pre-resolved list threaded from the dispatch (#3206) is used as-is; the
    # per-dispatch resolver must not run a second time inside this call.
    with patch("teatree.agents.skill_bundle.active_overlay_stage_skills") as resolver:
        present = stage_skills_present(
            _task("coding"), ["rules", "backend-dev", "code"], configured=["backend-dev", "absent"]
        )
    assert present == ["backend-dev"]
    resolver.assert_not_called()


def test_stage_precedence_line_names_the_skills_and_is_additive() -> None:
    line = stage_precedence_line(["backend-dev", "frontend-dev"])
    assert "backend-dev" in line
    assert "frontend-dev" in line
    assert "ADDITIVE" in line
    assert "authoritative" in line
