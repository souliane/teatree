"""Tests for :class:`GlabGhMrStateClassifier` — the shell-out MR-state classifier (#1131).

A GitHub URL routes to ``gh pr view`` and a GitLab URL to ``glab mr view -R``;
a merged state is a verdict, a non-forge URL is a deterministic non-match
(``merged=False``), and a subprocess failure (rc≠0, missing binary, garbage
JSON) raises :class:`ScannerError` rather than mis-reporting the MR as open.
"""

import subprocess

import pytest

from teatree.loop.scanners.base import ScannerError
from teatree.loop.scanners.slack_broadcast_mr_classifier import GlabGhMrStateClassifier

GH_URL = "https://github.com/team/project/pull/42"
GL_URL = "https://gitlab.example.com/team/project/-/merge_requests/7"


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["tool"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestVerdicts:
    def test_merged_github_pr_is_a_merged_and_approved_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "teatree.utils.run.run_allowed_to_fail",
            lambda *a, **k: _completed('{"state": "MERGED", "reviewDecision": "APPROVED", "author": {"login": "me"}}'),
        )
        [state] = GlabGhMrStateClassifier(github_token="ghp-fake")([GH_URL])
        assert state.merged is True
        assert state.approved is True
        assert state.author_username == "me"

    def test_open_gitlab_mr_is_an_unmerged_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "teatree.utils.run.run_allowed_to_fail",
            lambda *a, **k: _completed('{"state": "opened", "upvotes": 0, "author": {"username": "colleague"}}'),
        )
        [state] = GlabGhMrStateClassifier(glab_token="glpat-fake")([GL_URL])
        assert state.merged is False
        assert state.approved is False
        assert state.author_username == "colleague"

    def test_non_forge_url_is_a_deterministic_non_match(self) -> None:
        [state] = GlabGhMrStateClassifier()(["https://example.com/not/an/mr"])
        assert state.merged is False
        assert state.approved is False


class TestFailureIsNotAVerdict:
    def test_nonzero_return_code_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "teatree.utils.run.run_allowed_to_fail",
            lambda *a, **k: _completed("", returncode=1, stderr="token expired"),
        )
        with pytest.raises(ScannerError):
            GlabGhMrStateClassifier(github_token="ghp-fake")([GH_URL])

    def test_garbage_json_raises_scanner_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "teatree.utils.run.run_allowed_to_fail",
            lambda *a, **k: _completed("not json at all"),
        )
        with pytest.raises(ScannerError):
            GlabGhMrStateClassifier(glab_token="glpat-fake")([GL_URL])
