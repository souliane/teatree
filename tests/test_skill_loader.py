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
    _detect_end_of_session,
    _detect_keyword_intent,
    _detect_url_intent,
    _parse_skill_requires,
    read_companion_skills,
    read_supplementary_skills,
    resolve_dependencies,
    suggest_skills,
)

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


class TestURLIntentDetection:
    def test_gitlab_issue(self):
        assert _detect_url_intent("check https://gitlab.com/org/repo/-/issues/123") == "t3-ticket"

    def test_gitlab_merge_request(self):
        assert _detect_url_intent("https://gitlab.com/org/repo/-/merge_requests/456") == "t3-ticket"

    def test_github_issue(self):
        assert _detect_url_intent("https://github.com/org/repo/issues/789") == "t3-ticket"

    def test_github_pull(self):
        assert _detect_url_intent("https://github.com/org/repo/pull/101") == "t3-ticket"

    def test_sentry(self):
        assert _detect_url_intent("https://sentry.io/issues/999") == "t3-debug"

    def test_notion(self):
        assert _detect_url_intent("https://www.notion.so/page-id") == "t3-ticket"

    def test_no_url(self):
        assert _detect_url_intent("just some text") == ""


class TestKeywordIntentDetection:
    def test_review(self):
        assert _detect_keyword_intent("review these MRs") == "t3-review"

    def test_ship(self):
        assert _detect_keyword_intent("commit and push") == "t3-ship"

    def test_ship_not_review(self):
        assert _detect_keyword_intent("review the commit") == "t3-review"

    def test_test(self):
        assert _detect_keyword_intent("run the tests") == "t3-test"

    def test_debug(self):
        assert _detect_keyword_intent("the app is broken") == "t3-debug"

    def test_code(self):
        assert _detect_keyword_intent("implement the login feature") == "t3-code"

    def test_code_imperative(self):
        assert _detect_keyword_intent("fix the login bug") == "t3-code"

    def test_ticket(self):
        assert _detect_keyword_intent("start working on PROJ-123") == "t3-ticket"

    def test_workspace(self):
        assert _detect_keyword_intent("start the backend") == "t3-workspace"

    def test_retro(self):
        assert _detect_keyword_intent("let's do a retro") == "t3-retro"

    def test_followup(self):
        assert _detect_keyword_intent("check ticket status") == "t3-followup"

    def test_no_match(self):
        assert _detect_keyword_intent("hello") == ""


class TestEndOfSession:
    def test_done(self):
        assert _detect_end_of_session("done") is True

    def test_lgtm(self):
        assert _detect_end_of_session("lgtm") is True

    def test_not_end(self):
        assert _detect_end_of_session("fix the bug") is False


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
        cache.write_text(json.dumps({"companion_skills": ["ac-django"]}))
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
        cache.write_text(json.dumps({"companion_skills": ["ac-django"]}))
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
