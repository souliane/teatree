from subprocess import CompletedProcess

import pytest

from teatree.utils import git_guard
from teatree.utils import run as utils_run_mod


class TestGuardRepoRemoteSlug:
    def test_raises_when_slug_does_not_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a[0], 0, "git@github.com:other-org/other-repo.git\n", ""),
        )
        with pytest.raises(ValueError, match="other-org/other-repo"):
            git_guard.guard_repo_remote_slug(repo="/tmp/r", expected_slug="souliane/teatree")

    def test_raises_names_expected_slug_in_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a[0], 0, "git@github.com:other-org/other-repo.git\n", ""),
        )
        with pytest.raises(ValueError, match="souliane/teatree"):
            git_guard.guard_repo_remote_slug(repo="/tmp/r", expected_slug="souliane/teatree")

    def test_passes_when_slug_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a[0], 0, "git@github.com:souliane/teatree.git\n", ""),
        )
        git_guard.guard_repo_remote_slug(repo="/tmp/r", expected_slug="souliane/teatree")

    def test_passes_with_https_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a[0], 0, "https://github.com/souliane/teatree.git\n", ""),
        )
        git_guard.guard_repo_remote_slug(repo="/tmp/r", expected_slug="souliane/teatree")

    def test_raises_when_remote_slug_is_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            utils_run_mod.subprocess,
            "run",
            lambda *a, **kw: CompletedProcess(a[0], 1, "", "no remote"),
        )
        with pytest.raises(ValueError, match="souliane/teatree"):
            git_guard.guard_repo_remote_slug(repo="/tmp/r", expected_slug="souliane/teatree")


class TestIsGithubSlug:
    def test_owner_repo_is_a_slug(self) -> None:
        assert git_guard.is_github_slug("souliane/teatree") is True

    def test_bare_basename_is_not_a_slug(self) -> None:
        assert git_guard.is_github_slug("teatree") is False

    def test_nested_namespace_is_not_a_two_part_slug(self) -> None:
        assert git_guard.is_github_slug("acme/team/backend") is False

    def test_empty_is_not_a_slug(self) -> None:
        assert git_guard.is_github_slug("") is False

    def test_missing_owner_is_not_a_slug(self) -> None:
        assert git_guard.is_github_slug("/teatree") is False

    def test_missing_name_is_not_a_slug(self) -> None:
        assert git_guard.is_github_slug("souliane/") is False
