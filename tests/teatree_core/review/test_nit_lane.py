"""NIT comments and their issue are tied to the same pull request."""

from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import PrReview
from teatree.core.review.nit_lane import (
    NitLaneError,
    NitPoint,
    _create_follow_up_issue,
    _post_missing,
    nit_point_marker,
    nit_pr_marker,
    render_follow_up_body,
    track_nits,
)
from teatree.utils.url_slug import pr_ref_from_url

PRIVATE_PATH = "/home/example/private.txt"  # privacy-scan:allow (fake fixture path, not PII)


class _Host:
    def __init__(self) -> None:
        self.threads: list[dict[str, object]] = []
        self.review_posts: list[PrReview] = []
        self.issues: dict[str, str] = {}
        self.issue_labels: list[str] = []
        self.label_shape = "names"
        self.thread_shape = "github"
        self.drop_create_labels = False
        self.persist_labels = True
        self.review_error = ""
        self.resolved_ids: list[str] = []
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
            if self.thread_shape == "gitlab":
                self.threads.append(
                    {"id": f"thread-{number}", "resolved": False, "notes": [{"body": comment.body, "id": number}]}
                )
            else:
                self.threads.append(
                    {
                        "id": f"thread-{number}",
                        "isResolved": False,
                        "notes": [{"body": comment.body, "url": f"https://github.com/o/r/pull/9#discussion_r{number}"}],
                    }
                )
        return (
            {"error": self.review_error}
            if self.review_error
            else {"html_url": "https://github.com/o/r/pull/9#pullrequestreview-7"}
        )

    def list_repo_open_issues(self, *, repo: str) -> list[dict[str, object]]:
        key = "web_url" if self.thread_shape == "gitlab" else "html_url"
        return [{key: url, "body": body} for url, body in self.issues.items()]

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, object]:
        url = (
            "https://gitlab.com/group/repo/-/issues/10"
            if self.thread_shape == "gitlab"
            else "https://github.com/o/r/issues/10"
        )
        self.issues[url] = body
        self.issue_labels = [] if self.drop_create_labels else labels or []
        key = "web_url" if self.thread_shape == "gitlab" else "html_url"
        return {key: url, "number": 10}

    def get_issue(self, issue_url: str) -> dict[str, object]:
        labels: object = self.issue_labels
        if self.label_shape == "dicts":
            labels = [{"name": label} for label in self.issue_labels]
        elif self.label_shape == "none_once":
            labels = None
            self.label_shape = "names"
        key = "web_url" if self.thread_shape == "gitlab" else "html_url"
        return {key: issue_url, "body": self.issues[issue_url], "labels": labels}

    def add_issue_labels(self, *, issue_url: str, labels: tuple[str, ...]) -> dict[str, object]:
        if self.persist_labels:
            self.issue_labels = sorted(set(self.issue_labels) | set(labels))
        return self.get_issue(issue_url)

    def update_issue(self, *, issue_url: str, body: str) -> dict[str, object]:
        self.issues[issue_url] = body
        return self.get_issue(issue_url)

    def resolve_review_thread(self, *, repo: str, pr_iid: int, thread_id: str) -> dict[str, object]:
        for thread in self.threads:
            if thread["id"] == thread_id:
                key = "resolved" if self.thread_shape == "gitlab" else "isResolved"
                thread[key] = True
                self.resolved_ids.append(thread_id)
                return {"id": thread_id, key: True}
        raise ValueError(thread_id)


@contextmanager
def _publication_contexts() -> Generator[list[tuple[str, bool]]]:
    contexts: list[tuple[str, bool]] = []

    def publish(**kwargs: Any) -> Any:
        context = kwargs["context"]
        contexts.append((str(kwargs["action"]), context.own_mr))
        return kwargs["publish"]()

    with (
        patch("teatree.core.review.nit_lane.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch("teatree.core.issue_hygiene.route_forge_write", side_effect=lambda **kw: kw["text"]),
        patch("teatree.core.review.nit_lane.require_self_authored_issue"),
        patch("teatree.core.issue_hygiene.require_self_authored_issue"),
        patch("teatree.core.review.nit_lane.require_on_behalf_approval", side_effect=publish),
        patch("teatree.core.on_behalf_gate_recorded.require_on_behalf_approval", side_effect=publish),
    ):
        yield contexts


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


def test_follow_up_labels_read_back_from_forge_label_dicts() -> None:
    host = _Host()
    host.label_shape = "dicts"
    host.issue_labels = ["nit"]
    pr_url = "https://github.com/o/r/pull/9"
    issue_url = "https://github.com/o/r/issues/10"
    point = NitPoint(file="", line=0, summary="Rename helper")
    host.issues[issue_url] = render_follow_up_body(
        pr_url=pr_url,
        marker=nit_pr_marker(pr_url),
        points=(NitPoint(file="PR", line=0, summary=point.summary, permalink=pr_url),),
    )

    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        patch("teatree.core.review.nit_lane.require_self_authored_issue"),
        patch("teatree.core.review.nit_lane.require_on_behalf_approval", side_effect=lambda **kw: kw["publish"]()),
        patch.object(host, "add_issue_labels", wraps=host.add_issue_labels) as add_labels,
    ):
        receipt = track_nits(host=host, pr_url=pr_url, head_sha="a" * 40, points=(point,))

    add_labels.assert_called_once_with(issue_url=issue_url, labels=("t3-auto",))
    assert host.get_issue(issue_url)["labels"] == [{"name": "nit"}, {"name": "t3-auto"}]
    assert receipt.issue_url == issue_url


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


class TestRealOutboundLeak(TestCase):
    def test_real_outbound_leak_is_a_recoverable_nit_lane_error(self) -> None:
        host = _Host()
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.core.gates.privacy_gate._db_banned_terms", return_value=[]),
            patch("teatree.core.gates.privacy_gate._registered_overlay_rules_union", return_value=([], [])),
            patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
            patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
            pytest.raises(NitLaneError, match="generic:home_path"),
        ):
            track_nits(
                host=host,
                pr_url="https://github.com/souliane/teatree/pull/9",
                head_sha="a" * 40,
                points=(NitPoint(file="x.py", line=2, summary=f"Inspect {PRIVATE_PATH}"),),
            )
        assert host.review_posts == []
        assert host.issues == {}

    def test_issue_only_privacy_refusal_is_a_recoverable_nit_lane_error(self) -> None:
        host = _Host()
        with (
            patch(
                "teatree.core.send_proxy.route_send",
                side_effect=lambda request: SimpleNamespace(allowed=True, payload=request.payload),
            ),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.core.gates.privacy_gate._db_banned_terms", return_value=[]),
            patch("teatree.core.gates.privacy_gate._registered_overlay_rules_union", return_value=([], [])),
            patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
            patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
            pytest.raises(NitLaneError, match="generic:home_path"),
        ):
            track_nits(
                host=host,
                pr_url="https://github.com/souliane/teatree/pull/9",
                head_sha="a" * 40,
                points=(NitPoint(file=PRIVATE_PATH, line=0, summary="Rename helper"),),
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


def test_github_nit_comment_uses_github_scrub_policy() -> None:
    host = _Host()
    with (
        patch("teatree.core.review.nit_lane.route_forge_write", side_effect=lambda **kw: kw["text"]) as scrub,
        patch("teatree.core.review.nit_lane.require_on_behalf_approval", side_effect=lambda **kw: kw["publish"]()),
    ):
        _post_missing(
            host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint("x.py", 2, "Rename helper"),),
            own_pr=True,
        )
    assert scrub.call_args.kwargs["forge"] == "github"
    assert len(host.review_posts) == 1


def test_post_missing_keeps_review_refusal_for_todo_anchored_comment() -> None:
    host = _Host()
    with (
        patch.object(host, "get_pr_file_diffs", return_value={"x.py": "@@ -1 +1 @@\n+# TODO later"}),
        patch("teatree.core.review.nit_lane.route_forge_write", side_effect=lambda **kw: kw["text"]),
        pytest.raises(NitLaneError, match="TODO-anchored blocker"),
    ):
        _post_missing(
            host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint("x.py", 1, "must be fixed"),),
            own_pr=True,
        )
    assert host.review_posts == []


@pytest.mark.parametrize("self_authored", [False, True])
def test_authorship_reaches_inline_and_label_approval_contexts(*, self_authored: bool) -> None:
    host = _Host()
    host.drop_create_labels = True
    point = NitPoint("x.py", 2, "Rename helper")
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=self_authored),
        _publication_contexts() as contexts,
    ):
        track_nits(host=host, pr_url="https://github.com/o/r/pull/9", head_sha="a" * 40, points=(point,))

    assert ("post_review_nit", self_authored) in contexts
    assert ("nit_follow_up_labels", self_authored) in contexts


def test_unconfirmed_authorship_refuses_publication() -> None:
    host = _Host()
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value=None),
        pytest.raises(NitLaneError, match="NIT review authorship could not be confirmed"),
    ):
        track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint("x.py", 2, "Rename helper"),),
        )
    assert host.review_posts == []


def test_gitlab_thread_urls_and_resolved_state_survive_readback() -> None:
    host = _Host()
    host.thread_shape = "gitlab"
    pr_url = "https://gitlab.com/group/repo/-/merge_requests/9"
    first = NitPoint("x.py", 2, "Rename helper")
    second = NitPoint("y.py", 3, "Move comment")
    first_url = f"{pr_url}#note_41"
    host.threads = [
        {
            "id": "thread-1",
            "resolved": True,
            "notes": [{"body": nit_point_marker(pr_url, "a" * 40, first), "web_url": first_url}],
        }
    ]
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        _publication_contexts(),
    ):
        receipt = track_nits(host=host, pr_url=pr_url, head_sha="a" * 40, points=(first, second))

    body = host.issues[receipt.issue_url]
    assert f"[x.py:2]({first_url})" in body
    assert f"[y.py:3]({pr_url}#note_2)" in body
    assert host.resolved_ids == ["thread-2"]
    assert receipt.thread_ids == ("thread-1", "thread-2")


def test_non_list_labels_are_added_and_read_back() -> None:
    host = _Host()
    host.label_shape = "none_once"
    host.drop_create_labels = True
    point = NitPoint("", 0, "Rename helper")
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        _publication_contexts(),
        patch.object(host, "add_issue_labels", wraps=host.add_issue_labels) as add_labels,
    ):
        receipt = track_nits(host=host, pr_url="https://github.com/o/r/pull/9", head_sha="a" * 40, points=(point,))
    add_labels.assert_called_once_with(issue_url=receipt.issue_url, labels=("nit", "t3-auto"))
    assert host.issue_labels == ["nit", "t3-auto"]


def test_moved_head_refuses_before_writing() -> None:
    host = _Host()
    with (
        patch.object(host, "fetch_live_head_sha", return_value="b" * 40),
        pytest.raises(NitLaneError, match="NIT review head moved before publication"),
    ):
        track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint("x.py", 2, "Rename helper"),),
        )
    assert host.review_posts == []


def test_inline_review_error_refuses_follow_up() -> None:
    host = _Host()
    host.review_error = "review unavailable"
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        _publication_contexts(),
        pytest.raises(NitLaneError, match="Inline NIT review refused: review unavailable"),
    ):
        track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint("x.py", 2, "Rename helper"),),
        )
    assert host.issues == {}


def test_duplicate_follow_up_marker_refuses_publication() -> None:
    host = _Host()
    marker = nit_pr_marker("https://github.com/o/r/pull/9")
    host.issues = {
        "https://github.com/o/r/issues/10": marker,
        "https://github.com/o/r/issues/11": marker,
    }
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        pytest.raises(NitLaneError, match="Multiple NIT follow-up issues carry this pull request marker"),
    ):
        track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint("", 0, "Rename helper"),),
        )
    assert host.review_posts == []


def test_follow_up_labels_that_do_not_persist_refuse_receipt() -> None:
    host = _Host()
    host.drop_create_labels = True
    host.persist_labels = False
    with (
        patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
        patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        _publication_contexts(),
        pytest.raises(NitLaneError, match="NIT follow-up issue labels did not read back"),
    ):
        track_nits(
            host=host,
            pr_url="https://github.com/o/r/pull/9",
            head_sha="a" * 40,
            points=(NitPoint("", 0, "Rename helper"),),
        )
