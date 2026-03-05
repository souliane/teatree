"""Tests for detect_tenant.py."""

import os
from unittest.mock import patch

from detect_tenant import (
    _default_tenants,
    _detect_from_description,
    _detect_from_labels,
    _fetch_issue,
    detect,
)
from lib.gitlab import ProjectInfo


class TestDetectFromLabels:
    def test_matches_case_insensitive(self) -> None:
        assert _detect_from_labels(["BUG", "Acme"], ["acme"]) == "acme"

    def test_no_match(self) -> None:
        assert _detect_from_labels(["bug"], ["acme"]) is None

    def test_empty_labels(self) -> None:
        assert _detect_from_labels([], ["acme"]) is None

    def test_empty_tenants(self) -> None:
        assert _detect_from_labels(["acme"], []) is None


class TestDetectFromDescription:
    def test_matches_case_insensitive(self) -> None:
        assert _detect_from_description("This is for Acme", ["acme"]) == "acme"

    def test_no_match(self) -> None:
        assert _detect_from_description("no tenant here", ["acme"]) is None

    def test_empty_description(self) -> None:
        assert _detect_from_description("", ["acme"]) is None


class TestFetchIssue:
    def test_empty_url(self) -> None:
        result = _fetch_issue("")
        assert result["source"] == "none"

    def test_bad_url(self) -> None:
        result = _fetch_issue("https://example.com/bad")
        assert result["source"] == "parse_error"

    def test_project_not_resolved(self) -> None:
        with patch("detect_tenant.resolve_project", return_value=None):
            result = _fetch_issue("https://gitlab.com/org/repo/-/issues/1")
        assert result["source"] == "project_error"

    def test_issue_not_found(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        with (
            patch("detect_tenant.resolve_project", return_value=proj),
            patch("detect_tenant.get_issue", return_value=None),
        ):
            result = _fetch_issue("https://gitlab.com/org/repo/-/issues/1")
        assert result["source"] == "issue_error"

    def test_success(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": ["acme"], "description": "text"}
        with (
            patch("detect_tenant.resolve_project", return_value=proj),
            patch("detect_tenant.get_issue", return_value=issue),
        ):
            result = _fetch_issue("https://gitlab.com/org/repo/-/issues/1")
        assert result == issue

    def test_work_items_url(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": [], "description": ""}
        with (
            patch("detect_tenant.resolve_project", return_value=proj),
            patch("detect_tenant.get_issue", return_value=issue),
        ):
            result = _fetch_issue("https://gitlab.com/org/repo/-/work_items/5")
        assert result == issue


class TestDefaultTenants:
    def test_from_env(self) -> None:
        os.environ["T3_KNOWN_TENANTS"] = "alpha, beta ,gamma"
        try:
            result = _default_tenants()
            assert result == ["alpha", "beta", "gamma"]
        finally:
            del os.environ["T3_KNOWN_TENANTS"]

    def test_empty_env(self) -> None:
        os.environ.pop("T3_KNOWN_TENANTS", None)
        assert _default_tenants() == []


class TestDetect:
    def test_explicit_tenant(self) -> None:
        result = detect(explicit="acme")
        assert result["tenant"] == "acme"
        assert result["source"] == "explicit"
        assert result["confidence"] == "high"

    def test_from_label(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": ["acme"], "description": ""}
        with (
            patch("detect_tenant.resolve_project", return_value=proj),
            patch("detect_tenant.get_issue", return_value=issue),
        ):
            result = detect(
                "https://gitlab.com/org/repo/-/issues/1",
                known_tenants=["acme"],
            )
        assert result["tenant"] == "acme"
        assert result["source"] == "label"

    def test_from_description(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": [], "description": "This is for globex"}
        with (
            patch("detect_tenant.resolve_project", return_value=proj),
            patch("detect_tenant.get_issue", return_value=issue),
        ):
            result = detect(
                "https://gitlab.com/org/repo/-/issues/1",
                known_tenants=["globex"],
            )
        assert result["tenant"] == "globex"
        assert result["source"] == "description"

    def test_not_found(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": [], "description": "nothing"}
        with (
            patch("detect_tenant.resolve_project", return_value=proj),
            patch("detect_tenant.get_issue", return_value=issue),
        ):
            result = detect(
                "https://gitlab.com/org/repo/-/issues/1",
                known_tenants=["acme"],
            )
        assert result["tenant"] == ""
        assert result["source"] == "not_found"

    def test_fetch_error_short_circuits(self) -> None:
        """When _fetch_issue returns a result dict with 'source', detect returns it directly."""
        result = detect("")
        assert result["source"] == "none"

    def test_uses_default_tenants_when_none(self) -> None:
        os.environ.pop("T3_KNOWN_TENANTS", None)
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": [], "description": "text"}
        with (
            patch("detect_tenant.resolve_project", return_value=proj),
            patch("detect_tenant.get_issue", return_value=issue),
        ):
            result = detect("https://gitlab.com/org/repo/-/issues/1")
        assert result["source"] == "not_found"

    def test_description_none(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {"labels": [], "description": None}
        with (
            patch("detect_tenant.resolve_project", return_value=proj),
            patch("detect_tenant.get_issue", return_value=issue),
        ):
            result = detect(
                "https://gitlab.com/org/repo/-/issues/1",
                known_tenants=["acme"],
            )
        assert result["source"] == "not_found"
