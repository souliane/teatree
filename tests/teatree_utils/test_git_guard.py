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
