"""``confirm_pr_head``: the open PR's head must be the sha the push just delivered."""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.forge_pr_probe import GithubOpenPrHead, PrProbe
from teatree.core.push.pr_head_check import PrHeadStatus, confirm_pr_head
from tests._git_repo import make_git_repo, run_git

PUSHED = "a" * 40
LAGGING = "b" * 40
PR_URL = "https://github.com/souliane/teatree/pull/9"


def _open_pr(head: str) -> GithubOpenPrHead:
    return GithubOpenPrHead(PR_URL, "souliane/teatree", "feature", head)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = make_git_repo(tmp_path / "clone")
    run_git(path, "remote", "add", "origin", "https://github.com/souliane/teatree.git")
    return path


def _confirm(repo: Path, *, head: GithubOpenPrHead | None, probe: PrProbe | None = None):
    with (
        patch("teatree.core.push.pr_head_check.github_open_pr_head", return_value=head),
        patch("teatree.core.push.pr_head_check.find_open_pr_for_branch", return_value=probe or PrProbe.unknown()),
    ):
        return confirm_pr_head(repo, "feature", PUSHED, settle_seconds=0)


class TestConfirmPrHead:
    def test_an_open_pr_at_the_pushed_sha_matches(self, repo: Path) -> None:
        verdict = _confirm(repo, head=_open_pr(PUSHED))

        assert verdict.status is PrHeadStatus.MATCHES
        assert verdict.ok

    def test_a_lagging_open_pr_head_is_refused_naming_both_shas(self, repo: Path) -> None:
        verdict = _confirm(repo, head=_open_pr(LAGGING))

        assert verdict.status is PrHeadStatus.LAGS
        assert not verdict.ok
        assert PUSHED in verdict.detail
        assert LAGGING in verdict.detail
        assert PR_URL in verdict.detail

    def test_a_head_that_catches_up_within_the_settle_window_matches(self, repo: Path) -> None:
        reads = [_open_pr(LAGGING), _open_pr(LAGGING), _open_pr(PUSHED)]
        with patch("teatree.core.push.pr_head_check.github_open_pr_head", side_effect=reads) as read:
            verdict = confirm_pr_head(repo, "feature", PUSHED, settle_seconds=0)

        assert verdict.status is PrHeadStatus.MATCHES
        assert read.call_count == 3

    def test_a_head_that_never_catches_up_lags_after_a_bounded_number_of_reads(self, repo: Path) -> None:
        with patch("teatree.core.push.pr_head_check.github_open_pr_head", return_value=_open_pr(LAGGING)) as read:
            verdict = confirm_pr_head(repo, "feature", PUSHED, settle_seconds=0)

        assert verdict.status is PrHeadStatus.LAGS
        assert read.call_count == 4

    def test_no_open_pr_for_the_branch_passes_with_a_note(self, repo: Path) -> None:
        verdict = _confirm(repo, head=None, probe=PrProbe.none())

        assert verdict.status is PrHeadStatus.NO_OPEN_PR
        assert verdict.ok
        assert "feature" in verdict.detail

    def test_an_unreadable_head_is_unverifiable_and_not_ok(self, repo: Path) -> None:
        verdict = _confirm(repo, head=None, probe=PrProbe.unknown())

        assert verdict.status is PrHeadStatus.UNVERIFIABLE
        assert not verdict.ok

    def test_an_open_pr_whose_head_cannot_be_read_is_unverifiable(self, repo: Path) -> None:
        verdict = _confirm(repo, head=None, probe=PrProbe.found(PR_URL))

        assert verdict.status is PrHeadStatus.UNVERIFIABLE
        assert PR_URL in verdict.detail

    def test_a_gitlab_remote_is_not_checked_and_does_not_block(self, tmp_path: Path) -> None:
        path = make_git_repo(tmp_path / "gitlab-clone")
        run_git(path, "remote", "add", "origin", "https://gitlab.com/group/project.git")

        verdict = _confirm(path, head=None, probe=PrProbe.found("https://gitlab.com/group/project/-/merge_requests/3"))

        assert verdict.status is PrHeadStatus.NOT_CHECKED
        assert verdict.ok
