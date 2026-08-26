"""``t3 review checkout`` — the CLI seam over the verify-or-fail cold-review checkout.

The helper (``teatree.utils.review_checkout``) had tests and a skill reference
but no runnable entry point, so no reviewer ever called it. These tests pin the
seam end to end against a real git origin: the success path materialises the
exact pushed head, a divergent ``--sha`` hard-fails rather than handing back a
stale tree, and both failure branches emit the structured JSON the sibling
``t3 review run`` contract uses.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli.review import review_app
from teatree.cli.review.checkout import head_ref_for
from teatree.utils.git import head_sha
from tests._git_repo import make_git_repo, run_git

PR_URL = "https://github.com/souliane/teatree/pull/42"
MR_URL = "https://gitlab.com/org/proj/-/merge_requests/42"


@pytest.fixture
def clone_with_pushed_head(tmp_path: Path) -> tuple[Path, str]:
    """A clone whose ``origin`` publishes the reviewed head under ``refs/pull/42/head``."""
    origin = make_git_repo(tmp_path / "origin.git", bare=True)

    seed = make_git_repo(tmp_path / "seed")
    (seed / "README.md").write_text("base\n")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-q", "-m", "base")
    run_git(seed, "remote", "add", "origin", str(origin))
    run_git(seed, "push", "-q", "origin", "main")
    (seed / "README.md").write_text("reviewed head\n")
    run_git(seed, "commit", "-q", "-am", "the reviewed head")
    pushed_head = run_git(seed, "rev-parse", "HEAD")
    run_git(seed, "push", "-q", "origin", "HEAD:refs/pull/42/head")

    clone = make_git_repo(tmp_path / "clone", initial_commit=False)
    run_git(clone, "remote", "add", "origin", str(origin))
    run_git(clone, "fetch", "-q", "origin")
    return clone, pushed_head


class TestHeadRefForForge:
    """The head ref is derived from the URL — no forge API call, no token."""

    def test_github_pr_url_maps_to_the_pull_head_ref(self) -> None:
        assert head_ref_for(PR_URL) == "refs/pull/42/head"

    def test_gitlab_mr_url_maps_to_the_merge_request_head_ref(self) -> None:
        assert head_ref_for(MR_URL) == "refs/merge-requests/42/head"

    def test_unparsable_url_yields_no_ref(self) -> None:
        assert head_ref_for("not-a-url") == ""


class TestReviewCheckoutSucceeds:
    def test_materialises_the_exact_reviewed_head_and_prints_its_path(
        self, clone_with_pushed_head: tuple[Path, str], tmp_path: Path
    ) -> None:
        clone, pushed_head = clone_with_pushed_head
        review_root = tmp_path / "review-roots"
        review_root.mkdir()

        result = CliRunner().invoke(
            review_app,
            ["checkout", PR_URL, "--sha", pushed_head, "--repo", str(clone), "--base-dir", str(review_root)],
        )

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["ref"] == "refs/pull/42/head"
        assert payload["sha"] == pushed_head
        assert payload["url"] == PR_URL
        assert head_sha(payload["worktree"]) == pushed_head
        assert (Path(payload["worktree"]) / "README.md").read_text() == "reviewed head\n"


class TestReviewCheckoutRefusesDivergence:
    def test_wrong_sha_exits_one_with_stale_checkout_and_no_worktree_path(
        self, clone_with_pushed_head: tuple[Path, str], tmp_path: Path
    ) -> None:
        clone, _pushed_head = clone_with_pushed_head
        review_root = tmp_path / "review-roots"
        review_root.mkdir()

        result = CliRunner().invoke(
            review_app,
            ["checkout", PR_URL, "--sha", "0" * 40, "--repo", str(clone), "--base-dir", str(review_root)],
        )

        assert result.exit_code == 1, f"output={result.output!r} exc={result.exception!r}"
        payload = json.loads(result.output.strip())
        assert payload["error"] == "stale_checkout"
        # A reviewer must never receive a tree the command could not prove is the head.
        assert "worktree" not in payload


class TestReviewCheckoutRefusesBadInput:
    def test_unparsable_url_exits_two_before_touching_git(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(review_app, ["checkout", "not-a-url", "--sha", "0" * 40, "--repo", str(tmp_path)])

        assert result.exit_code == 2, f"output={result.output!r} exc={result.exception!r}"
        assert json.loads(result.output.strip())["error"] == "bad_url"

    def test_unfetchable_ref_exits_one_with_checkout_failed(self, tmp_path: Path) -> None:
        clone = make_git_repo(tmp_path / "clone")

        result = CliRunner().invoke(review_app, ["checkout", PR_URL, "--sha", "0" * 40, "--repo", str(clone)])

        assert result.exit_code == 1, f"output={result.output!r} exc={result.exception!r}"
        assert json.loads(result.output.strip())["error"] == "checkout_failed"


class TestReviewRelease:
    """``t3 review release`` — the counterpart ``checkout`` structurally cannot run (#4576)."""

    def test_release_deregisters_the_review_worktree(
        self, clone_with_pushed_head: tuple[Path, str], tmp_path: Path
    ) -> None:
        clone, pushed_head = clone_with_pushed_head
        review_root = tmp_path / "review-root"
        review_root.mkdir()
        checkout = CliRunner().invoke(
            review_app,
            ["checkout", PR_URL, "--sha", pushed_head, "--repo", str(clone), "--base-dir", str(review_root)],
        )
        worktree = json.loads(checkout.output.strip())["worktree"]

        result = CliRunner().invoke(review_app, ["release", worktree, "--repo", str(clone)])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        assert json.loads(result.output.strip())["released"] == worktree
        assert worktree not in run_git(clone, "worktree", "list", "--porcelain")

    def test_release_of_an_unregistered_path_exits_one(self, tmp_path: Path) -> None:
        clone = make_git_repo(tmp_path / "clone")

        result = CliRunner().invoke(review_app, ["release", str(tmp_path / "nope"), "--repo", str(clone)])

        assert result.exit_code == 1, f"output={result.output!r} exc={result.exception!r}"
        assert json.loads(result.output.strip())["error"] == "release_failed"
