"""Tests for scripts/lib/skill_loader.py."""

from __future__ import annotations  # noqa: TID251 — test for standalone script

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# skill_loader lives in scripts/lib/, add scripts/ to path
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib.skill_loader import (  # noqa: E402
    _parse_skill_requires,
    build_trigger_index,
    detect_intent,
    parse_triggers_from_frontmatter,
    read_companion_skills,
    read_supplementary_skills,
    resolve_dependencies,
    suggest_skills,
)

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


# ── Trigger frontmatter parsing ──────────────────────────────────────


class TestParseTriggers:
    def test_full_triggers(self):
        md = (
            "---\nname: t3-ship\ntriggers:\n  priority: 10\n  exclude: '\\breview\\b'\n"
            "  keywords:\n    - '\\bcommit\\b'\n    - '\\bpush\\b'\n  urls:\n"
            "    - 'https?://example.com'\n---\n# Ship"
        )
        result = parse_triggers_from_frontmatter(md)
        assert result is not None
        assert result["priority"] == 10
        assert result["exclude"] == r"\breview\b"
        assert result["keywords"] == [r"\bcommit\b", r"\bpush\b"]
        assert result["urls"] == ["https?://example.com"]
        assert result["end_of_session"] is False

    def test_end_of_session(self):
        md = (
            "---\nname: t3-retro\ntriggers:\n  priority: 100\n"
            "  end_of_session: true\n  keywords:\n    - '\\bretro\\b'\n---\n"
        )
        result = parse_triggers_from_frontmatter(md)
        assert result is not None
        assert result["end_of_session"] is True

    def test_no_triggers(self):
        md = "---\nname: t3-rules\n---\n# Rules"
        assert parse_triggers_from_frontmatter(md) is None

    def test_no_frontmatter(self):
        assert parse_triggers_from_frontmatter("# No frontmatter") is None

    def test_default_priority(self):
        md = "---\nname: test\ntriggers:\n  keywords:\n    - '\\btest\\b'\n---\n"
        result = parse_triggers_from_frontmatter(md)
        assert result is not None
        assert result["priority"] == 50

    def test_triggers_block_terminated_by_next_key(self):
        md = "---\nname: test\ntriggers:\n  priority: 5\n  keywords:\n    - '\\bfoo\\b'\nmetadata:\n  version: 1\n---\n"
        result = parse_triggers_from_frontmatter(md)
        assert result is not None
        assert result["keywords"] == [r"\bfoo\b"]


# ── Build trigger index from skills directory ────────────────────────


class TestBuildTriggerIndex:
    def test_builds_from_skills_dir(self):
        if not SKILLS_DIR.is_dir():
            pytest.skip("skills directory not found")
        index = build_trigger_index([SKILLS_DIR])
        # At minimum, t3-ship (priority 10) should be first
        assert len(index) > 0
        assert index[0]["skill"] == "t3-ship"
        assert index[0]["priority"] == 10

    def test_sorted_by_priority(self):
        if not SKILLS_DIR.is_dir():
            pytest.skip("skills directory not found")
        index = build_trigger_index([SKILLS_DIR])
        priorities = [e["priority"] for e in index]
        assert priorities == sorted(priorities)

    def test_empty_dir(self, tmp_path):
        assert build_trigger_index([tmp_path]) == []

    def test_skill_without_triggers_excluded(self, tmp_path):
        skill_dir = tmp_path / "no-triggers"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: no-triggers\n---\n# No triggers")
        assert build_trigger_index([tmp_path]) == []


# ── Intent detection (data-driven) ──────────────────────────────────


class TestDetectIntent:
    @pytest.fixture
    def trigger_index(self):
        """Minimal trigger index for testing."""
        return [
            {
                "skill": "t3-ship",
                "priority": 10,
                "exclude": r"\breview\b",
                "keywords": [r"\b(commit|push)\b"],
                "urls": [],
                "end_of_session": False,
            },
            {
                "skill": "t3-test",
                "priority": 20,
                "exclude": "",
                "keywords": [r"\b(pytest|run.*tests?)\b"],
                "urls": [],
                "end_of_session": False,
            },
            {
                "skill": "t3-review",
                "priority": 40,
                "exclude": "",
                "keywords": [r"\breview\b"],
                "urls": [],
                "end_of_session": False,
            },
            {
                "skill": "t3-debug",
                "priority": 50,
                "exclude": "",
                "keywords": [r"\b(broken|error)\b"],
                "urls": ["https?://[^\\s]*sentry\\.[^\\s]+/issues/"],
                "end_of_session": False,
            },
            {
                "skill": "t3-ticket",
                "priority": 60,
                "exclude": "",
                "keywords": [r"([a-z]+-\d+)"],
                "urls": ["https?://gitlab\\.[^\\s]+/-/issues/\\d+"],
                "end_of_session": False,
            },
            {
                "skill": "t3-code",
                "priority": 70,
                "exclude": "",
                "keywords": [r"\b(implement|refactor)\b"],
                "urls": [],
                "end_of_session": False,
            },
            {
                "skill": "t3-retro",
                "priority": 100,
                "exclude": "",
                "keywords": [r"\bretro\b"],
                "urls": [],
                "end_of_session": True,
            },
        ]

    def test_keyword_match(self, trigger_index):
        assert detect_intent("commit and push", trigger_index=trigger_index) == "t3-ship"

    def test_exclude_prevents_match(self, trigger_index):
        assert detect_intent("review the commit", trigger_index=trigger_index) == "t3-review"

    def test_url_match_takes_priority(self, trigger_index):
        assert (
            detect_intent("check https://gitlab.com/org/repo/-/issues/123", trigger_index=trigger_index) == "t3-ticket"
        )

    def test_sentry_url(self, trigger_index):
        assert detect_intent("https://sentry.io/issues/999", trigger_index=trigger_index) == "t3-debug"

    def test_test_intent(self, trigger_index):
        assert detect_intent("run the tests", trigger_index=trigger_index) == "t3-test"

    def test_code_intent(self, trigger_index):
        assert detect_intent("implement the login feature", trigger_index=trigger_index) == "t3-code"

    def test_ticket_intent(self, trigger_index):
        assert detect_intent("start working on PROJ-123", trigger_index=trigger_index) == "t3-ticket"

    def test_no_match(self, trigger_index):
        assert detect_intent("hello", trigger_index=trigger_index) == ""

    def test_end_of_session(self, trigger_index):
        result = detect_intent(
            "done",
            trigger_index=trigger_index,
            loaded_skills={"t3-code", "t3-workspace"},
        )
        assert result == "t3-retro"

    def test_end_of_session_no_lifecycle_loaded(self, trigger_index):
        result = detect_intent("done", trigger_index=trigger_index, loaded_skills=set())
        assert result == ""

    def test_end_of_session_retro_already_loaded(self, trigger_index):
        result = detect_intent(
            "done",
            trigger_index=trigger_index,
            loaded_skills={"t3-code", "t3-retro"},
        )
        assert result == ""

    def test_retro_keyword(self, trigger_index):
        assert detect_intent("let's do a retro", trigger_index=trigger_index) == "t3-retro"

    def test_empty_index(self):
        assert detect_intent("commit", trigger_index=[]) == ""

    def test_falls_back_to_skill_dirs(self, tmp_path):
        """When no cache, builds index from skill_search_dirs."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ntriggers:\n  keywords:\n    - '\\bhello\\b'\n---\n")
        with mock.patch("lib.skill_loader._read_trigger_index", return_value=[]):
            result = detect_intent("hello", skill_search_dirs=[tmp_path])
        assert result == "my-skill"


# ── Integration: detect_intent with real SKILL.md triggers ───────────


class TestDetectIntentIntegration:
    """Test intent detection against real SKILL.md trigger patterns."""

    @pytest.fixture
    def real_index(self):
        if not SKILLS_DIR.is_dir():
            pytest.skip("skills directory not found")
        return build_trigger_index([SKILLS_DIR])

    def test_ship(self, real_index):
        assert detect_intent("commit and push", trigger_index=real_index) == "t3-ship"

    def test_ship_blocked_by_review(self, real_index):
        assert detect_intent("review the commit", trigger_index=real_index) == "t3-review"

    def test_test(self, real_index):
        assert detect_intent("run the tests", trigger_index=real_index) == "t3-test"

    def test_review(self, real_index):
        assert detect_intent("review these MRs", trigger_index=real_index) == "t3-review"

    def test_debug(self, real_index):
        assert detect_intent("the app is broken", trigger_index=real_index) == "t3-debug"

    def test_ticket(self, real_index):
        assert detect_intent("start working on PROJ-123", trigger_index=real_index) == "t3-ticket"

    def test_code(self, real_index):
        assert detect_intent("implement the login feature", trigger_index=real_index) == "t3-code"

    def test_code_imperative(self, real_index):
        assert detect_intent("fix the login bug", trigger_index=real_index) == "t3-code"

    def test_workspace(self, real_index):
        assert detect_intent("start the backend", trigger_index=real_index) == "t3-workspace"

    def test_retro(self, real_index):
        assert detect_intent("let's do a retro", trigger_index=real_index) == "t3-retro"

    def test_followup(self, real_index):
        assert detect_intent("check ticket status", trigger_index=real_index) == "t3-followup"

    def test_gitlab_url(self, real_index):
        assert detect_intent("check https://gitlab.com/org/repo/-/issues/123", trigger_index=real_index) == "t3-ticket"

    def test_sentry_url(self, real_index):
        assert detect_intent("https://sentry.io/issues/999", trigger_index=real_index) == "t3-debug"

    def test_no_match(self, real_index):
        assert detect_intent("hello", trigger_index=real_index) == ""


# ── Preserved tests ──────────────────────────────────────────────────


class TestParseSkillRequires:
    def test_with_requires(self):
        md = "---\nname: t3-review\nrequires:\n  - t3-workspace\n  - t3-platforms\n---\n# Review"
        assert _parse_skill_requires(md) == ["t3-workspace", "t3-platforms"]

    def test_no_requires(self):
        md = "---\nname: t3-setup\n---\n# Setup"
        assert _parse_skill_requires(md) == []

    def test_no_frontmatter(self):
        assert _parse_skill_requires("# No frontmatter") == []


class TestResolveDependencies:
    def test_resolves_from_real_skills(self):
        if not SKILLS_DIR.is_dir():
            pytest.skip("skills directory not found")
        resolved = resolve_dependencies(["t3-review"], [SKILLS_DIR])
        assert "t3-workspace" in resolved
        assert "t3-platforms" in resolved
        assert resolved.index("t3-workspace") < resolved.index("t3-review")

    def test_unknown_skill(self):
        resolved = resolve_dependencies(["nonexistent-skill"], [SKILLS_DIR])
        assert resolved == ["nonexistent-skill"]


class TestCompanionSkills:
    def test_reads_cache(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(json.dumps({"companion_skills": ["ac-django", "ac-python"]}))
        with mock.patch("lib.skill_loader.SKILL_METADATA_CACHE", cache):
            assert read_companion_skills() == ["ac-django", "ac-python"]

    def test_missing_cache(self, tmp_path):
        with mock.patch("lib.skill_loader.SKILL_METADATA_CACHE", tmp_path / "missing.json"):
            assert read_companion_skills() == []

    def test_corrupt_cache(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text("not json")
        with mock.patch("lib.skill_loader.SKILL_METADATA_CACHE", cache):
            assert read_companion_skills() == []


class TestSupplementarySkills:
    def test_reads_config(self, tmp_path):
        config = tmp_path / "skills.yml"
        config.write_text("ac-django: '.'\nac-ruff: '\\b(ruff)\\b'\n")
        assert read_supplementary_skills(str(config), "hello") == ["ac-django"]
        assert read_supplementary_skills(str(config), "adopt ruff") == ["ac-django", "ac-ruff"]

    def test_missing_config(self):
        assert read_supplementary_skills("/nonexistent", "hello") == []

    def test_comments_and_blanks(self, tmp_path):
        config = tmp_path / "skills.yml"
        config.write_text("# comment\n\nac-django: '.'\n")
        assert read_supplementary_skills(str(config), "hello") == ["ac-django"]


class TestSuggestSkills:
    def test_review_includes_companions(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(
            json.dumps(
                {
                    "companion_skills": ["ac-django"],
                    "trigger_index": [
                        {
                            "skill": "t3-review",
                            "priority": 40,
                            "keywords": [r"\breview\b"],
                            "urls": [],
                            "exclude": "",
                            "end_of_session": False,
                        },
                    ],
                }
            )
        )
        with mock.patch("lib.skill_loader.SKILL_METADATA_CACHE", cache):
            result = suggest_skills(
                {
                    "prompt": "review these MRs",
                    "cwd": ".",
                    "loaded_skills": [],
                    "skill_search_dirs": [str(SKILLS_DIR)],
                    "supplementary_config": "",
                }
            )
        assert "t3-review" in result["suggestions"]
        assert "ac-django" in result["suggestions"]
        assert result["intent"] == "t3-review"

    def test_filters_loaded(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(
            json.dumps(
                {
                    "companion_skills": ["ac-django"],
                    "trigger_index": [
                        {
                            "skill": "t3-review",
                            "priority": 40,
                            "keywords": [r"\breview\b"],
                            "urls": [],
                            "exclude": "",
                            "end_of_session": False,
                        },
                    ],
                }
            )
        )
        with mock.patch("lib.skill_loader.SKILL_METADATA_CACHE", cache):
            result = suggest_skills(
                {
                    "prompt": "review code",
                    "cwd": ".",
                    "loaded_skills": ["t3-review", "ac-django"],
                    "skill_search_dirs": [str(SKILLS_DIR)],
                    "supplementary_config": "",
                }
            )
        assert "t3-review" not in result["suggestions"]
        assert "ac-django" not in result["suggestions"]

    def test_no_intent(self):
        result = suggest_skills(
            {
                "prompt": "hello",
                "cwd": ".",
                "loaded_skills": [],
                "skill_search_dirs": [],
                "supplementary_config": "",
            }
        )
        assert result["suggestions"] == []
        assert result["intent"] == ""
