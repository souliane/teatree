"""NIT comments and their issue are tied to the same pull request."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.core.backend_protocols import PrReview
from teatree.core.review.nit_lane import (
    NitLaneError,
    NitPoint,
    _create_follow_up_issue,
    _post_missing,
    render_follow_up_body,
    track_nits,
)
from teatree.utils.url_slug import pr_ref_from_url


class _Host:
    def __init__(self) -> None:
        self.threads: list[dict[str, object]] = []
        self.review_posts: list[PrReview] = []
        self.issues: dict[str, str] = {}
        self.issue_labels: list[str] = []
        self.drop_threads_after_issue = False

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        return "a" * 40

    def list_review_threads(self, *, repo: str, pr_iid: int) -> list[dict[str, object]]:
        if self.drop_threads_after_issue and self.issues:
            return []
        return self.threads

    def get_pr_file_diffs(self, *, repo: str, pr_iid: int) -> dict[str, str]:
        return {"x.py": "@@ -1 +1,3 @@\n+one\n+two\n+three", "y.py": "@@ -1 +1,3 @@\n+one\n+two\n+three"}

    def submit_pr_review(self, *, repo: str, pr_iid: int, review: PrReview) -> dict[str, object]:
        self.review_posts.append(review)
        for comment in review.comments:
            number = len(self.threads) + 1
            self.threads.append(
                {
                    "id": f"thread-{number}",
                    "isResolved": False,
                    "notes": [{"body": comment.body, "url": f"https://github.com/o/r/pull/9#discussion_r{number}"}],
                }
            )
        return {"html_url": "https://github.com/o/r/pull/9#pullrequestreview-7"}

    def list_repo_open_issues(self, *, repo: str) -> list[dict[str, object]]:
        return [{"html_url": url, "body": body} for url, body in self.issues.items()]

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, object]:
        url = "https://github.com/o/r/issues/10"
        self.issues[url] = body
        self.issue_labels = labels or []
        return {"html_url": url, "number": 10}

    def get_issue(self, issue_url: str) -> dict[str, object]:
        return {"html_url": issue_url, "body": self.issues[issue_url], "labels": self.issue_labels}

    def add_issue_labels(self, *, issue_url: str, labels: tuple[str, ...]) -> dict[str, object]:
        self.issue_labels = sorted(set(self.issue_labels) | set(labels))
        return self.get_issue(issue_url)

    def update_issue(self, *, issue_url: str, body: str) -> dict[str, object]:
        self.issues[issue_url] = body
        return self.get_issue(issue_url)

    def resolve_review_thread(self, *, repo: str, pr_iid: int, thread_id: str) -> dict[str, object]:
        for thread in self.threads:
            if thread["id"] == thread_id:
                thread["isResolved"] = True
                return {"id": thread_id, "isResolved": True}
        raise ValueError(thread_id)


def test_follow_up_body_has_only_header_inline_permalinks_and_closing_sentence() -> None:
    body = render_follow_up_body(
        pr_url="https://github.com/o/r/pull/9",
        marker="<!-- t3-nit-pr:abc -->",
        points=(
            NitPoint(
                file="x.py", line=2, summary="Rename helper", permalink="https://github.com/o/r/pull/9#discussion_r7"
            ),
            NitPoint(
                file="y.py", line=3, summary="Move comment", permalink="https://github.com/o/r/pull/9#discussion_r8"
            ),
        ),
    )

    assert body.splitlines()[0] == "Non-blocking points from https://github.com/o/r/pull/9 <!-- t3-nit-pr:abc -->"
    assert "1. [x.py:2](https://github.com/o/r/pull/9#discussion_r7) — Rename helper" in body
    assert "2. [y.py:3](https://github.com/o/r/pull/9#discussion_r8) — Move comment" in body
    assert body.endswith("These points do not block the merge and are tracked in this issue.")
    assert "Rejected candidates" not in body


def test_track_nits_posts_each_inline_then_one_issue_and_resolves_threads() -> None:
    host = _Host()
    points = (
        NitPoint(file="x.py", line=2, summary="Rename helper"),
        NitPoint(file="y.py", line=3, summary="Move comment"),
    )
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        patch("teatree.core.review.nit_lane.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch("teatree.core.issue_hygiene.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch("teatree.core.review.nit_lane.require_self_authored_issue"),
        patch(
            "teatree.core.review.nit_lane.require_on_behalf_approval",
            side_effect=lambda **kw: kw["publish"](),
        ) as gate,
        patch(
            "teatree.core.on_behalf_gate_recorded.require_on_behalf_approval",
            side_effect=lambda **kw: kw["publish"](),
        ),
    ):
        receipt = track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=points,
        )
        host.issue_labels.clear()
        repeated = track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=points,
        )

    assert len(host.review_posts) == 1
    assert len(host.review_posts[0].comments) == 2
    assert host.issue_labels == ["nit", "t3-auto"]
    assert len(host.issues) == 1
    body = next(iter(host.issues.values()))
    assert "1. [x.py:2](https://github.com/o/r/pull/9#discussion_r1)" in body
    assert "2. [y.py:3](https://github.com/o/r/pull/9#discussion_r2)" in body
    assert "Rejected candidates" not in body
    assert all(thread["isResolved"] is True for thread in host.threads)
    assert receipt.issue_url == "https://github.com/o/r/issues/10"
    assert repeated == receipt
    assert any(call.kwargs.get("action") == "resolve_review_nit" for call in gate.call_args_list)


def test_later_nit_keeps_one_numbered_issue_body() -> None:
    host = _Host()
    first = NitPoint(file="x.py", line=2, summary="Rename helper")
    second = NitPoint(file="y.py", line=3, summary="Move comment")
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        patch("teatree.core.review.nit_lane.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch("teatree.core.issue_hygiene.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch("teatree.core.review.nit_lane.require_self_authored_issue"),
        patch("teatree.core.issue_hygiene.require_self_authored_issue"),
        patch(
            "teatree.core.on_behalf_gate_recorded.require_on_behalf_approval",
            side_effect=lambda **kw: kw["publish"](),
        ),
    ):
        track_nits(host=host, pr_url="https://github.com/o/r/pull/9", head_sha="a" * 40, points=(first,))
        track_nits(host=host, pr_url="https://github.com/o/r/pull/9", head_sha="a" * 40, points=(second,))
    body = next(iter(host.issues.values()))
    assert body.count("1. [x.py:2]") == 1
    assert "2. [y.py:3]" in body
    assert body.endswith("These points do not block the merge and are tracked in this issue.")
    assert "Request added" not in body


def test_thread_read_disappearing_after_issue_does_not_report_success() -> None:
    host = _Host()
    host.drop_threads_after_issue = True
    points = (NitPoint(file="x.py", line=2, summary="Rename helper"),)
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        patch("teatree.core.review.nit_lane.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch("teatree.core.issue_hygiene.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch(
            "teatree.core.on_behalf_gate_recorded.require_on_behalf_approval",
            side_effect=lambda **kw: kw["publish"](),
        ),
        pytest.raises(NitLaneError, match="resolution did not read back"),
    ):
        track_nits(host=host, pr_url="https://github.com/o/r/pull/9", head_sha="a" * 40, points=points)


def test_inline_review_never_posts_when_privacy_scan_refuses() -> None:
    host = _Host()
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        patch("teatree.core.review.nit_lane.route_forge_write", side_effect=RuntimeError("public text refused")),
        pytest.raises(RuntimeError, match="public text refused"),
    ):
        track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint(file="x.py", line=2, summary="rename helper"),),
        )
    assert host.review_posts == []
    assert host.issues == {}


def test_missing_or_off_diff_anchor_becomes_issue_only() -> None:
    host = _Host()
    points = (
        NitPoint(file="", line=0, summary="Rename helper"),
        NitPoint(file="x.py", line=99, summary="Move comment"),
    )
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        patch("teatree.core.issue_hygiene.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch(
            "teatree.core.on_behalf_gate_recorded.require_on_behalf_approval", side_effect=lambda **kw: kw["publish"]()
        ),
    ):
        receipt = track_nits(host=host, pr_url="https://github.com/o/r/pull/9", head_sha="a" * 40, points=points)
    assert host.review_posts == []
    assert len(host.issues) == 1
    assert len(receipt.thread_ids) == 2


def test_nit_comment_checks_refuse_unbacked_claim_before_posting() -> None:
    host = _Host()
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        pytest.raises(NitLaneError, match="unbacked claim"),
    ):
        track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint(file="x.py", line=2, summary="This is missing a test"),),
        )
    assert host.review_posts == []


def test_gitlab_multi_thread_issue_does_not_resolve_unrelated_discussions() -> None:
    ref = pr_ref_from_url("https://gitlab.com/group/repo/-/merge_requests/9")
    assert ref is not None
    with patch(
        "teatree.core.review.nit_lane.create_or_extend_by_marker",
        return_value=SimpleNamespace(kind="created_new", issue_url="https://gitlab.com/group/repo/-/issues/10"),
    ) as create:
        _create_follow_up_issue(_Host(), ref, thread_ids=("thread-1", "thread-2"), body="body", marker="marker")
    draft = create.call_args.kwargs["draft"]
    assert draft.merge_request_to_resolve_discussions_of is None
    assert draft.discussion_to_resolve == ""


def test_gitlab_nit_comment_uses_gitlab_scrub_policy() -> None:
    host = _Host()
    with (
        patch("teatree.core.review.nit_lane.route_forge_write", side_effect=lambda **kw: kw["text"]) as scrub,
        patch("teatree.core.review.nit_lane.require_on_behalf_approval", side_effect=lambda **kw: kw["publish"]()),
    ):
        _post_missing(
            host,
            pr_url="https://gitlab.com/group/repo/-/merge_requests/9",
            head_sha="a" * 40,
            points=(NitPoint("x.py", 2, "Rename helper"),),
            own_pr=True,
        )
    assert scrub.call_args.kwargs["forge"] == "gitlab"
