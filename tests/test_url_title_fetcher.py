"""Tests for teatree.url_title_fetcher."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree import url_title_fetcher as utf
from teatree.config import get_effective_settings
from teatree.core.models import ConfigSetting


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    cache = tmp_path / "url-titles.json"
    monkeypatch.setattr(utf, "CACHE_FILE", cache)
    return cache


def _mk_completed(rc: int, stdout: str) -> SimpleNamespace:
    """Build a stand-in for subprocess.run's CompletedProcess."""
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


class TestExtractJobs:
    def test_finds_gitlab_merge_request(self):
        prompt = "review https://gitlab.com/group/repo/-/merge_requests/42"
        jobs = utf._extract_jobs(prompt)
        assert len(jobs) == 1
        assert jobs[0][0] == "gitlab:group/repo:merge_requests:42"

    def test_finds_gitlab_issue_and_work_item(self):
        prompt = "context: https://gitlab.com/group/repo/-/issues/7 and https://gitlab.com/group/repo/-/work_items/8"
        jobs = utf._extract_jobs(prompt)
        keys = [j[0] for j in jobs]
        assert "gitlab:group/repo:issues:7" in keys
        # work_items normalized to issues for cache symmetry
        assert "gitlab:group/repo:issues:8" in keys

    def test_finds_github_pr_and_issue(self):
        prompt = "https://github.com/owner/repo/pull/12 https://github.com/owner/repo/issues/3"
        jobs = utf._extract_jobs(prompt)
        keys = [j[0] for j in jobs]
        assert "github:owner/repo:pull:12" in keys
        assert "github:owner/repo:issues:3" in keys

    def test_caps_at_max_urls(self):
        urls = " ".join(f"https://gitlab.com/g/r/-/merge_requests/{i}" for i in range(20))
        assert len(utf._extract_jobs(urls)) == utf.MAX_URLS

    def test_returns_empty_for_no_urls(self):
        assert utf._extract_jobs("just a plain prompt with no urls") == []


class TestFetchTitles:
    def test_disabled_via_env_var(self, monkeypatch, cache_path):
        monkeypatch.setenv("T3_HOOK_FETCH_TITLES", "0")
        assert utf.fetch_titles("https://gitlab.com/g/r/-/merge_requests/1") == []

    def test_returns_cached_titles_without_subprocess(self, cache_path):
        cache_path.write_text(json.dumps({"gitlab:g/r:merge_requests:1": "Cached title"}))
        with patch("teatree.url_title_fetcher.run_allowed_to_fail") as run:
            titles = utf.fetch_titles("https://gitlab.com/g/r/-/merge_requests/1")
        assert titles == ["Cached title"]
        run.assert_not_called()

    def test_fetches_uncached_gitlab_title(self, cache_path):
        with (
            patch("teatree.url_title_fetcher.shutil.which", return_value="/usr/bin/glab"),
            patch(
                "teatree.url_title_fetcher.run_allowed_to_fail",
                return_value=_mk_completed(0, json.dumps({"title": "Real MR title"})),
            ),
        ):
            titles = utf.fetch_titles("https://gitlab.com/g/r/-/merge_requests/99")
        assert titles == ["Real MR title"]
        cached = json.loads(cache_path.read_text())
        assert cached["gitlab:g/r:merge_requests:99"] == "Real MR title"

    def test_fetches_uncached_github_pr_title(self, cache_path):
        with (
            patch("teatree.url_title_fetcher.shutil.which", return_value="/usr/bin/gh"),
            patch(
                "teatree.url_title_fetcher.run_allowed_to_fail",
                return_value=_mk_completed(0, json.dumps({"title": "Real PR title"})),
            ),
        ):
            titles = utf.fetch_titles("https://github.com/owner/repo/pull/5")
        assert titles == ["Real PR title"]

    def test_failed_fetch_does_not_cache(self, cache_path):
        with (
            patch("teatree.url_title_fetcher.shutil.which", return_value="/usr/bin/glab"),
            patch(
                "teatree.url_title_fetcher.run_allowed_to_fail",
                return_value=_mk_completed(1, ""),
            ),
        ):
            titles = utf.fetch_titles("https://gitlab.com/g/r/-/merge_requests/99")
        assert titles == []
        assert not cache_path.is_file() or cache_path.read_text().strip() in {"", "{}", "{}\n"}

    def test_glab_missing_returns_empty(self, cache_path):
        with patch("teatree.url_title_fetcher.shutil.which", return_value=None):
            titles = utf.fetch_titles("https://gitlab.com/g/r/-/merge_requests/1")
        assert titles == []


class TestFetchTitlesDbHome(TestCase):
    """``hook_fetch_titles`` is DB-home (#2697) — the DB row governs the fetch."""

    @pytest.fixture(autouse=True)
    def _stage(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cache_path = tmp_path / "url-titles.json"
        monkeypatch.setattr(utf, "CACHE_FILE", self.cache_path)
        monkeypatch.delenv("T3_HOOK_FETCH_TITLES", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_disabled_via_db_setting(self) -> None:
        # Anti-vacuous: a cached title WOULD be returned when enabled; the DB
        # off-row must short-circuit to [] before the cache is even consulted.
        ConfigSetting.objects.set_value("hook_fetch_titles", value=False)
        self.cache_path.write_text(json.dumps({"gitlab:g/r:merge_requests:1": "Cached title"}))
        assert utf.fetch_titles("https://gitlab.com/g/r/-/merge_requests/1") == []

    def test_enabled_by_default_with_no_db_row(self) -> None:
        self.cache_path.write_text(json.dumps({"gitlab:g/r:merge_requests:1": "Cached title"}))
        assert utf.fetch_titles("https://gitlab.com/g/r/-/merge_requests/1") == ["Cached title"]


class TestModuleScopeConfigImport:
    def test_get_effective_settings_imported_at_module_scope(self):
        # The deferred function-scoped form required a PLC0415 suppression that ruff
        # autofix could strip then re-flag; importing at module scope removes that.
        assert utf.get_effective_settings is get_effective_settings


class TestEnrichPrompt:
    def test_appends_titles_to_prompt(self, cache_path):
        cache_path.write_text(json.dumps({"gitlab:g/r:merge_requests:1": "feat(demo-svc): joint rep"}))
        result = utf.enrich_prompt("review https://gitlab.com/g/r/-/merge_requests/1")
        assert "review https://gitlab.com/g/r/-/merge_requests/1" in result
        assert "[linked title: feat(demo-svc): joint rep]" in result

    def test_returns_prompt_unchanged_when_no_titles(self, cache_path, monkeypatch):
        monkeypatch.setenv("T3_HOOK_FETCH_TITLES", "0")
        prompt = "https://gitlab.com/g/r/-/merge_requests/1"
        assert utf.enrich_prompt(prompt) == prompt

    def test_returns_prompt_unchanged_when_no_urls(self):
        assert utf.enrich_prompt("just a prompt") == "just a prompt"
