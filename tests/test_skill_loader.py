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

from lib import skill_loader as skill_loader_mod  # noqa: E402
from lib.skill_loader import (  # noqa: E402
    build_trigger_index,
    detect_intent,
    parse_triggers_from_frontmatter,
    read_companion_skills,
    read_supplementary_skills,
    suggest_skills,
)

import teatree.skill_loading as skill_loading_mod  # noqa: E402

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


# ── Trigger frontmatter parsing ──────────────────────────────────────


class TestParseTriggers:
    def test_full_triggers(self):
        md = (
            "---\nname: ship\ntriggers:\n  priority: 10\n  exclude: '\\breview\\b'\n"
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
            "---\nname: retro\ntriggers:\n  priority: 100\n"
            "  end_of_session: true\n  keywords:\n    - '\\bretro\\b'\n---\n"
        )
        result = parse_triggers_from_frontmatter(md)
        assert result is not None
        assert result["end_of_session"] is True

    def test_no_triggers(self):
        md = "---\nname: rules\n---\n# Rules"
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
        # At minimum, t3:ship (priority 10) should be first
        assert len(index) > 0
        assert index[0]["skill"] == "ship"
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
                "skill": "ship",
                "priority": 10,
                "exclude": r"\breview\b",
                "keywords": [r"\b(commit|push)\b"],
                "urls": [],
                "end_of_session": False,
            },
            {
                "skill": "test",
                "priority": 20,
                "exclude": "",
                "keywords": [r"\b(pytest|run.*tests?)\b"],
                "urls": [],
                "end_of_session": False,
            },
            {
                "skill": "review",
                "priority": 40,
                "exclude": "",
                "keywords": [r"\breview\b"],
                "urls": [],
                "end_of_session": False,
            },
            {
                "skill": "debug",
                "priority": 50,
                "exclude": "",
                "keywords": [r"\b(broken|error)\b"],
                "urls": ["https?://[^\\s]*sentry\\.[^\\s]+/issues/"],
                "end_of_session": False,
            },
            {
                "skill": "ticket",
                "priority": 60,
                "exclude": "",
                "keywords": [r"([a-z]+-\d+)"],
                "urls": ["https?://gitlab\\.[^\\s]+/-/issues/\\d+"],
                "end_of_session": False,
            },
            {
                "skill": "code",
                "priority": 70,
                "exclude": "",
                "keywords": [r"\b(implement|refactor)\b"],
                "urls": [],
                "end_of_session": False,
            },
            {
                "skill": "retro",
                "priority": 100,
                "exclude": "",
                "keywords": [r"\bretro\b"],
                "urls": [],
                "end_of_session": True,
            },
        ]

    def test_explicit_slash_command_overrides_url(self, trigger_index):
        result = detect_intent(
            "/t3:review https://gitlab.com/org/repo/-/merge_requests/190",
            trigger_index=trigger_index,
        )
        assert result == "review"

    def test_explicit_slash_command_without_leading_slash(self, trigger_index):
        assert detect_intent("ship some args", trigger_index=trigger_index) == "ship"

    def test_explicit_slash_command_unknown_skill(self, trigger_index):
        assert detect_intent("/unknown-skill do something", trigger_index=trigger_index) != "unknown-skill"

    def test_keyword_match(self, trigger_index):
        assert detect_intent("commit and push", trigger_index=trigger_index) == "ship"

    def test_exclude_prevents_match(self, trigger_index):
        assert detect_intent("review the commit", trigger_index=trigger_index) == "review"

    def test_url_match_takes_priority(self, trigger_index):
        assert detect_intent("check https://gitlab.com/org/repo/-/issues/123", trigger_index=trigger_index) == "ticket"

    def test_sentry_url(self, trigger_index):
        assert detect_intent("https://sentry.io/issues/999", trigger_index=trigger_index) == "debug"

    def test_test_intent(self, trigger_index):
        assert detect_intent("run the tests", trigger_index=trigger_index) == "test"

    def test_code_intent(self, trigger_index):
        assert detect_intent("implement the login feature", trigger_index=trigger_index) == "code"

    def test_ticket_intent(self, trigger_index):
        assert detect_intent("start working on PROJ-123", trigger_index=trigger_index) == "ticket"

    def test_no_match(self, trigger_index):
        assert detect_intent("hello", trigger_index=trigger_index) == ""

    def test_end_of_session(self, trigger_index):
        result = detect_intent(
            "done",
            trigger_index=trigger_index,
            loaded_skills={"code", "workspace"},
        )
        assert result == "retro"

    def test_end_of_session_no_lifecycle_loaded(self, trigger_index):
        result = detect_intent("done", trigger_index=trigger_index, loaded_skills=set())
        assert result == ""

    def test_end_of_session_retro_already_loaded(self, trigger_index):
        result = detect_intent(
            "done",
            trigger_index=trigger_index,
            loaded_skills={"code", "retro"},
        )
        assert result == ""

    def test_retro_keyword(self, trigger_index):
        assert detect_intent("let's do a retro", trigger_index=trigger_index) == "retro"

    def test_empty_index(self):
        assert detect_intent("commit", trigger_index=[]) == ""

    def test_falls_back_to_skill_dirs(self, tmp_path):
        """When no cache, builds index from skill_search_dirs."""
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\ntriggers:\n  keywords:\n    - '\\bhello\\b'\n---\n")
        with mock.patch.object(skill_loader_mod, "_read_trigger_index", return_value=[]):
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
        assert detect_intent("commit and push", trigger_index=real_index) == "ship"

    def test_ship_blocked_by_review(self, real_index):
        assert detect_intent("review the commit", trigger_index=real_index) == "review"

    def test_test(self, real_index):
        assert detect_intent("run the tests", trigger_index=real_index) == "test"

    def test_review(self, real_index):
        assert detect_intent("review these MRs", trigger_index=real_index) == "review"

    def test_debug(self, real_index):
        assert detect_intent("the app is broken", trigger_index=real_index) == "debug"

    def test_ticket(self, real_index):
        assert detect_intent("start working on PROJ-123", trigger_index=real_index) == "ticket"

    def test_code(self, real_index):
        assert detect_intent("implement the login feature", trigger_index=real_index) == "code"

    def test_code_imperative(self, real_index):
        assert detect_intent("fix the login bug", trigger_index=real_index) == "code"

    def test_workspace(self, real_index):
        assert detect_intent("start the backend", trigger_index=real_index) == "workspace"

    def test_retro(self, real_index):
        assert detect_intent("let's do a retro", trigger_index=real_index) == "retro"

    def test_followup(self, real_index):
        assert detect_intent("check ticket status", trigger_index=real_index) == "followup"

    def test_gitlab_url(self, real_index):
        assert detect_intent("check https://gitlab.com/org/repo/-/issues/123", trigger_index=real_index) == "ticket"

    def test_sentry_url(self, real_index):
        assert detect_intent("https://sentry.io/issues/999", trigger_index=real_index) == "debug"

    def test_no_match(self, real_index):
        assert detect_intent("hello", trigger_index=real_index) == ""


# ── Cache version validation ─────────────────────────────────────────


class TestMetadataCacheInvalidation:
    def test_valid_version_returns_data(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(json.dumps({"teatree_version": "1.0.0", "trigger_index": [{"skill": "test"}]}))
        with (
            mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache),
            mock.patch.object(skill_loader_mod, "_get_installed_version", return_value="1.0.0"),
        ):
            result = skill_loader_mod._read_metadata_cache()
            assert result["trigger_index"] == [{"skill": "test"}]

    def test_mismatched_version_returns_empty(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(json.dumps({"teatree_version": "1.0.0", "trigger_index": [{"skill": "test"}]}))
        with (
            mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache),
            mock.patch.object(skill_loader_mod, "_get_installed_version", return_value="2.0.0"),
        ):
            assert skill_loader_mod._read_metadata_cache() == {}

    def test_missing_version_in_cache_skips_check(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(json.dumps({"trigger_index": [{"skill": "test"}]}))
        with mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache):
            result = skill_loader_mod._read_metadata_cache()
            assert result["trigger_index"] == [{"skill": "test"}]


# ── Preserved tests ──────────────────────────────────────────────────


class TestCompanionSkills:
    def test_reads_cache(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(json.dumps({"companion_skills": ["ac-django", "ac-python"]}))
        with mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache):
            assert read_companion_skills() == ["ac-django", "ac-python"]

    def test_missing_cache(self, tmp_path):
        with mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", tmp_path / "missing.json"):
            assert read_companion_skills() == []

    def test_corrupt_cache(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text("not json")
        with mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache):
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
    def test_review_includes_framework_and_dependencies(self, tmp_path):
        (tmp_path / "manage.py").write_text("# django project\n", encoding="utf-8")
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(
            json.dumps(
                {
                    "trigger_index": [
                        {
                            "skill": "review",
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
        with mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache):
            result = suggest_skills(
                {
                    "prompt": "review these MRs",
                    "cwd": str(tmp_path),
                    "loaded_skills": [],
                    "skill_search_dirs": [str(SKILLS_DIR)],
                    "supplementary_config": "",
                }
            )
        assert "ac-django" in result["suggestions"]
        assert "review" in result["suggestions"]
        assert result["intent"] == "review"

    def test_filters_loaded(self, tmp_path):
        (tmp_path / "manage.py").write_text("# django project\n", encoding="utf-8")
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(
            json.dumps(
                {
                    "trigger_index": [
                        {
                            "skill": "review",
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
        with mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache):
            result = suggest_skills(
                {
                    "prompt": "review code",
                    "cwd": str(tmp_path),
                    "loaded_skills": ["review", "ac-django", "workspace", "platforms", "code"],
                    "skill_search_dirs": [str(SKILLS_DIR)],
                    "supplementary_config": "",
                }
            )
        assert "review" not in result["suggestions"]
        assert "ac-django" not in result["suggestions"]

    def test_overlay_skill_requires_remote_match(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(
            json.dumps(
                {
                    "skill_path": "skills/t3:acme/SKILL.md",
                    "remote_patterns": ["git@gitlab.com:acme-engineering/*"],
                    "trigger_index": [
                        {
                            "skill": "code",
                            "priority": 70,
                            "keywords": [r"\bimplement\b"],
                            "urls": [],
                            "exclude": "",
                            "end_of_session": False,
                        },
                    ],
                }
            )
        )
        with (
            mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache),
            mock.patch.object(
                skill_loading_mod.git,
                "remote_url",
                return_value="git@gitlab.com:acme-engineering/platform-product",
            ),
        ):
            result = suggest_skills(
                {
                    "prompt": "implement the change",
                    "cwd": str(tmp_path),
                    "loaded_skills": [],
                    "skill_search_dirs": [str(SKILLS_DIR)],
                    "supplementary_config": "",
                }
            )
        assert "skills/t3:acme/SKILL.md" in result["suggestions"]

    def test_vague_prompt_does_not_load_overlay_skill(self, tmp_path):
        cache = tmp_path / "skill-metadata.json"
        cache.write_text(
            json.dumps(
                {
                    "skill_path": "skills/t3:acme/SKILL.md",
                    "remote_patterns": ["git@gitlab.com:acme-engineering/*"],
                    "trigger_index": [],
                }
            )
        )
        with mock.patch.object(skill_loader_mod, "SKILL_METADATA_CACHE", cache):
            result = suggest_skills(
                {
                    "prompt": "hello",
                    "cwd": str(tmp_path),
                    "loaded_skills": [],
                    "skill_search_dirs": [str(SKILLS_DIR)],
                    "supplementary_config": "",
                }
            )
        assert result["suggestions"] == []

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
