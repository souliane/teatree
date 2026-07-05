"""Tests for ``teatree.loop.rendering_permalinks`` (#1113 enhancement)."""

from typing import ClassVar
from unittest.mock import patch

import pytest

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering_classification import _ClassifiedActions
from teatree.loop.rendering_items import _PRRef
from teatree.loop.rendering_permalinks import (
    _slack_permalink,
    build_review_post_permalinks,
    enrich_pr_refs_with_permalinks,
)


class TestSlackPermalinkBuilder:
    def test_builds_canonical_archive_url(self) -> None:
        assert _slack_permalink("C9", "1779.0001") == "https://slack.com/archives/C9/p17790001"

    def test_empty_channel_collapses_to_empty(self) -> None:
        assert _slack_permalink("", "1779.0001") == ""

    def test_empty_ts_collapses_to_empty(self) -> None:
        assert _slack_permalink("C9", "") == ""


class TestBuildReviewPostPermalinksNoUrls:
    def test_no_urls_short_circuits_without_querying(self) -> None:
        assert build_review_post_permalinks([]) == {}

    def test_actions_without_url_are_ignored(self) -> None:
        action = DispatchAction(kind="statusline", zone="in_flight", detail="x", payload={"overlay": "t3"})
        assert build_review_post_permalinks([action]) == {}

    def test_actions_outside_zones_are_ignored(self) -> None:
        action = DispatchAction(kind="statusline", zone="anchors", detail="x", payload={"url": "https://x/mr/1"})
        assert build_review_post_permalinks([action]) == {}

    def test_non_statusline_actions_are_ignored(self) -> None:
        action = DispatchAction(kind="agent", zone="t3:reviewer", detail="x", payload={"url": "https://x/mr/1"})
        assert build_review_post_permalinks([action]) == {}


class TestBuildReviewPostPermalinksDjangoErrors:
    """Django-not-ready / DB-error paths must fail open to an empty map."""

    _ACTIONS: ClassVar = [
        DispatchAction(
            kind="statusline",
            zone="in_flight",
            detail="PR #145",
            payload={"url": "https://x/mr/145", "iid": 145},
        ),
    ]

    def test_apps_get_model_failure_returns_empty(self) -> None:
        with patch("django.apps.apps.get_model", side_effect=LookupError("boom")):
            assert build_review_post_permalinks(self._ACTIONS) == {}

    def test_db_query_failure_returns_empty(self) -> None:
        from types import SimpleNamespace  # noqa: PLC0415

        def _raise(**_kwargs: object) -> object:
            msg = "db down"
            raise RuntimeError(msg)

        broken = SimpleNamespace(objects=SimpleNamespace(filter=_raise))
        with patch("django.apps.apps.get_model", return_value=broken):
            assert build_review_post_permalinks(self._ACTIONS) == {}


def test_enrich_pr_refs_with_permalinks_is_noop_for_empty_map() -> None:
    """Empty permalinks must leave the classified ref lists untouched."""
    c = _ClassifiedActions()
    ref = _PRRef(iid=1, url="https://x/mr/1")
    c.action_prs["t3"] = [ref]
    c.inflight_prs["t3"] = [ref]
    enrich_pr_refs_with_permalinks(c, {})
    assert c.action_prs["t3"][0] is ref
    assert c.inflight_prs["t3"][0] is ref


def test_enrich_pr_refs_with_permalinks_replaces_only_matching_urls() -> None:
    c = _ClassifiedActions()
    matched = _PRRef(iid=1, url="https://x/mr/1")
    other = _PRRef(iid=2, url="https://x/mr/2")
    c.inflight_prs["t3"] = [matched, other]
    enrich_pr_refs_with_permalinks(c, {"https://x/mr/1": "https://slack.com/archives/C9/p1"})
    refs = c.inflight_prs["t3"]
    assert refs[0].review_permalink == "https://slack.com/archives/C9/p1"
    assert refs[1].review_permalink == ""


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestBuildReviewPostPermalinksDB:
    def test_resolves_row_into_canonical_permalink(self) -> None:
        from teatree.core.models.review_request_post import ReviewRequestPost  # noqa: PLC0415

        url = "https://x/mr/145"
        ReviewRequestPost.objects.create(mr_url=url, slack_channel_id="C9", slack_thread_ts="1779.0001")
        actions = [
            DispatchAction(kind="statusline", zone="in_flight", detail="PR #145", payload={"url": url, "iid": 145}),
        ]
        assert build_review_post_permalinks(actions) == {url: "https://slack.com/archives/C9/p17790001"}

    def test_row_with_missing_channel_yields_no_permalink(self) -> None:
        from teatree.core.models.review_request_post import ReviewRequestPost  # noqa: PLC0415

        url = "https://x/mr/146"
        ReviewRequestPost.objects.create(mr_url=url, slack_channel_id="", slack_thread_ts="1779.0001")
        actions = [
            DispatchAction(kind="statusline", zone="in_flight", detail="PR #146", payload={"url": url, "iid": 146}),
        ]
        assert build_review_post_permalinks(actions) == {}
