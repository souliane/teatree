"""Tests for fetch_issue_context.py."""

from unittest.mock import patch

from fetch_issue_context import (
    _download_images,
    _extract_external_links,
    fetch_context,
)
from lib.gitlab import ProjectInfo


class TestExtractExternalLinks:
    def test_finds_notion_and_jira(self) -> None:
        text = "See https://notion.so/page-123 and https://www.atlassian.net/browse/X-1"
        links = _extract_external_links(text)
        assert len(links) == 2
        assert "https://notion.so/page-123" in links
        assert "https://www.atlassian.net/browse/X-1" in links

    def test_finds_linear(self) -> None:
        links = _extract_external_links("check https://linear.app/team/issue/123")
        assert len(links) == 1

    def test_deduplicates(self) -> None:
        text = "https://notion.so/x https://notion.so/x"
        assert len(_extract_external_links(text)) == 1

    def test_no_links(self) -> None:
        assert _extract_external_links("nothing here") == []

    def test_jira_subdomain(self) -> None:
        links = _extract_external_links("https://jira.example/browse/T-1")
        assert len(links) == 1

    def test_atlassian_subdomain(self) -> None:
        """atlassian.net with subdomain but no www prefix is NOT matched by the regex."""
        links = _extract_external_links("https://myorg.atlassian.net/browse/X-1")
        assert len(links) == 0


class TestDownloadImages:
    def test_downloads_matching_images(self) -> None:
        desc = "![alt](/uploads/abc/img.png) text ![other](/uploads/def/pic.jpg)"
        with patch("fetch_issue_context.download_file", return_value=True) as mock_dl:
            result = _download_images(desc, "https://gitlab.com/org/repo", "/tmp/imgs")
        assert len(result) == 2
        assert result[0]["alt"] == "alt"
        assert result[0]["local_path"] == "/tmp/imgs/img.png"
        assert mock_dl.call_count == 2

    def test_skips_failed_download(self) -> None:
        desc = "![x](/uploads/fail.png)"
        with patch("fetch_issue_context.download_file", return_value=False):
            result = _download_images(desc, "https://gitlab.com/org/repo", "/tmp")
        assert result == []

    def test_no_images(self) -> None:
        with patch("fetch_issue_context.download_file") as mock_dl:
            result = _download_images("no images", "https://gitlab.com/org/repo", "/tmp")
        assert result == []
        mock_dl.assert_not_called()


class TestFetchContext:
    def test_bad_url(self) -> None:
        result = fetch_context("https://example.com/not-a-gitlab-url")
        assert "error" in result

    def test_project_not_resolved(self) -> None:
        with patch("fetch_issue_context.resolve_project", return_value=None):
            result = fetch_context("https://gitlab.com/org/repo/-/issues/1")
        assert "error" in result
        assert "Could not resolve project" in result["error"]

    def test_issue_not_found(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        with (
            patch("fetch_issue_context.resolve_project", return_value=proj),
            patch("fetch_issue_context.get_issue", return_value=None),
        ):
            result = fetch_context("https://gitlab.com/org/repo/-/issues/99")
        assert "error" in result

    def test_successful_fetch_no_images(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {
            "web_url": "https://gitlab.com/org/repo/-/issues/5",
            "title": "Bug fix",
            "description": "plain text",
            "labels": ["bug", "Process::Doing"],
            "assignees": [{"username": "alice"}],
        }
        comments = [
            {"author": {"username": "bob"}, "body": "noted", "created_at": "2024-01-01"},
        ]
        with (
            patch("fetch_issue_context.resolve_project", return_value=proj),
            patch("fetch_issue_context.get_issue", return_value=issue),
            patch("fetch_issue_context.get_issue_comments", return_value=comments),
        ):
            result = fetch_context("https://gitlab.com/org/repo/-/issues/5", download_images=False)

        assert result["iid"] == 5
        assert result["title"] == "Bug fix"
        assert result["process_label"] == "Process::Doing"
        assert result["assignees"] == ["alice"]
        assert len(result["comments"]) == 1
        assert result["images"] == []

    def test_successful_fetch_with_images(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        desc = "![screenshot](/uploads/abc/shot.png)"
        issue = {
            "web_url": "https://gitlab.com/org/repo/-/issues/5",
            "title": "Issue",
            "description": desc,
            "labels": [],
            "assignees": [],
        }
        with (
            patch("fetch_issue_context.resolve_project", return_value=proj),
            patch("fetch_issue_context.get_issue", return_value=issue),
            patch("fetch_issue_context.get_issue_comments", return_value=[]),
            patch("fetch_issue_context.download_file", return_value=True),
        ):
            result = fetch_context("https://gitlab.com/org/repo/-/issues/5")

        assert len(result["images"]) == 1
        assert result["process_label"] is None

    def test_work_items_url(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {
            "web_url": "https://gitlab.com/org/repo/-/work_items/7",
            "title": "WI",
            "description": None,
            "labels": ["Process:: Review"],
            "assignees": [],
        }
        with (
            patch("fetch_issue_context.resolve_project", return_value=proj),
            patch("fetch_issue_context.get_issue", return_value=issue),
            patch("fetch_issue_context.get_issue_comments", return_value=[]),
        ):
            result = fetch_context("https://gitlab.com/org/repo/-/work_items/7")

        assert result["iid"] == 7
        assert result["description"] == ""
        assert result["process_label"] == "Process:: Review"

    def test_image_dir_used_when_provided(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        desc = "![x](/uploads/a/b.png)"
        issue = {
            "web_url": "https://gitlab.com/org/repo/-/issues/1",
            "title": "T",
            "description": desc,
            "labels": [],
            "assignees": [],
        }
        with (
            patch("fetch_issue_context.resolve_project", return_value=proj),
            patch("fetch_issue_context.get_issue", return_value=issue),
            patch("fetch_issue_context.get_issue_comments", return_value=[]),
            patch("fetch_issue_context.download_file", return_value=True),
        ):
            result = fetch_context(
                "https://gitlab.com/org/repo/-/issues/1",
                image_dir="/custom/dir",
            )
        assert result["images"][0]["local_path"] == "/custom/dir/b.png"

    def test_external_links_from_comments(self) -> None:
        proj = ProjectInfo(project_id=1, path_with_namespace="org/repo", short_name="repo")
        issue = {
            "web_url": "https://gitlab.com/org/repo/-/issues/1",
            "title": "T",
            "description": "desc",
            "labels": [],
            "assignees": [],
        }
        comments = [{"body": "See https://notion.so/page-abc", "author": {"username": "u"}, "created_at": ""}]
        with (
            patch("fetch_issue_context.resolve_project", return_value=proj),
            patch("fetch_issue_context.get_issue", return_value=issue),
            patch("fetch_issue_context.get_issue_comments", return_value=comments),
        ):
            result = fetch_context("https://gitlab.com/org/repo/-/issues/1", download_images=False)
        assert "https://notion.so/page-abc" in result["external_links"]
