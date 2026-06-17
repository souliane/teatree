"""Tests for ``teatree.core.dev_repo`` — overlay-self / core repo detection.

Covers issue #727: ``workspace ticket`` must provision the *self* repo
(overlay package or teatree core) when the issue URL belongs to it, not
the overlay's product repo set.
"""

import subprocess
from pathlib import Path

import pytest

from teatree.core.dev_repo import issue_url_slug, resolve_dev_repo, resolve_repo_names

_GIT = "git"


def _init_repo(path: Path, origin_slug: str) -> Path:
    """Create a git repo at *path* with an ``origin`` pointing at *origin_slug*."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run([_GIT, "-C", str(path), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        [_GIT, "-C", str(path), "remote", "add", "origin", f"git@github.com:{origin_slug}.git"],
        check=True,
    )
    return path


class TestIssueUrlSlug:
    def test_parses_github_issue_url(self) -> None:
        assert issue_url_slug("https://github.com/souliane/teatree/issues/727") == "souliane/teatree"

    def test_parses_github_pull_url(self) -> None:
        assert issue_url_slug("https://github.com/souliane/teatree/pull/42") == "souliane/teatree"

    def test_parses_gitlab_issue_url(self) -> None:
        assert issue_url_slug("https://gitlab.com/grp/sub/proj/-/issues/9") == "grp/sub/proj"

    def test_parses_gitlab_work_item_url(self) -> None:
        assert issue_url_slug("https://gitlab.com/grp/proj/-/work_items/9") == "grp/proj"

    def test_strips_trailing_slash(self) -> None:
        assert issue_url_slug("https://github.com/owner/repo/issues/1/") == "owner/repo"

    def test_returns_empty_for_unrecognised_url(self) -> None:
        assert issue_url_slug("https://example.com/issues/42") == ""

    def test_returns_empty_for_blank(self) -> None:
        assert issue_url_slug("") == ""


class _ProductOverlay:
    """Minimal overlay stub whose product repo set is unrelated to self/core."""

    def get_workspace_repos(self) -> list[str]:
        return ["backend", "frontend"]


class TestResolveRepoNames:
    def test_explicit_repos_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.dev_repo.resolve_dev_repo", lambda _url: "souliane/teatree")

        assert resolve_repo_names(_ProductOverlay(), "https://example.com/issues/1", " api , web ") == ["api", "web"]

    def test_dev_repo_match_returns_single_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.dev_repo.resolve_dev_repo", lambda _url: "souliane/teatree")

        assert resolve_repo_names(_ProductOverlay(), "https://github.com/souliane/teatree/issues/1", "") == [
            "souliane/teatree",
        ]

    def test_no_dev_repo_falls_back_to_product_repos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.dev_repo.resolve_dev_repo", lambda _url: None)

        assert resolve_repo_names(_ProductOverlay(), "https://github.com/acme/backend/issues/1", "") == [
            "backend",
            "frontend",
        ]


class TestResolveDevRepo:
    def test_core_issue_resolves_to_core_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        core = _init_repo(tmp_path / "souliane" / "teatree", "souliane/teatree")
        monkeypatch.setattr("teatree.core.dev_repo.find_project_root", lambda: core)
        monkeypatch.setattr("teatree.core.dev_repo.discover_active_overlay", lambda: None)

        resolved = resolve_dev_repo("https://github.com/souliane/teatree/issues/727")

        assert resolved == "souliane/teatree"

    def test_overlay_package_issue_resolves_to_overlay_repo(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core = _init_repo(tmp_path / "souliane" / "teatree", "souliane/teatree")
        pkg = _init_repo(tmp_path / "acme" / "t3-acme", "acme-org/t3-acme")

        class _Entry:
            project_path = pkg

        entry = _Entry()
        monkeypatch.setattr("teatree.core.dev_repo.find_project_root", lambda: core)
        monkeypatch.setattr("teatree.core.dev_repo.discover_active_overlay", lambda: entry)

        resolved = resolve_dev_repo("https://github.com/acme-org/t3-acme/issues/5")

        assert resolved == "acme-org/t3-acme"

    def test_product_issue_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        core = _init_repo(tmp_path / "souliane" / "teatree", "souliane/teatree")
        monkeypatch.setattr("teatree.core.dev_repo.find_project_root", lambda: core)
        monkeypatch.setattr("teatree.core.dev_repo.discover_active_overlay", lambda: None)

        resolved = resolve_dev_repo("https://github.com/acme/product-backend/issues/3")

        assert resolved is None

    def test_unrecognised_url_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        core = _init_repo(tmp_path / "souliane" / "teatree", "souliane/teatree")
        monkeypatch.setattr("teatree.core.dev_repo.find_project_root", lambda: core)
        monkeypatch.setattr("teatree.core.dev_repo.discover_active_overlay", lambda: None)

        assert resolve_dev_repo("https://example.com/issues/42") is None

    def test_no_project_root_and_no_overlay_path_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.dev_repo.find_project_root", lambda: None)
        monkeypatch.setattr("teatree.core.dev_repo.discover_active_overlay", lambda: None)

        assert resolve_dev_repo("https://github.com/souliane/teatree/issues/1") is None

    def test_overlay_entry_without_project_path_is_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        core = _init_repo(tmp_path / "souliane" / "teatree", "souliane/teatree")

        class _Entry:
            project_path = None

        entry = _Entry()
        monkeypatch.setattr("teatree.core.dev_repo.find_project_root", lambda: core)
        monkeypatch.setattr("teatree.core.dev_repo.discover_active_overlay", lambda: entry)

        assert resolve_dev_repo("https://github.com/souliane/teatree/issues/1") == "souliane/teatree"
