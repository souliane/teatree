"""Tests for the forge adapters' ``merge_pr_bound`` — the real except-block, not a port fake."""

from collections.abc import Callable, Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.core.merge.uncleared_merge as uncleared_mod
from teatree.core.merge import MergePreconditionError
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.models import MergeAudit
from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient
from teatree.loop.scanners.pr_sweep_gitlab import GlabPrApiClient
from teatree.loop.scanners.pr_sweep_types import BoundMergeResult
from teatree.utils.pr_ref import PrRef
from tests.teatree_core.conftest import record_owned_pr_for_test, seed_merge_safe_verdict

SLUG = "souliane/teatree"
HEAD = "331eaa1e3f0f6a4e2d1b585ea1178c9baad6de95"
REFUSAL = "no rubric is recorded for this ticket"


def _refuse(*, ref: PrRef, expected_head_oid: str, query: CodeHostQuery | None = None) -> str:
    raise MergePreconditionError(REFUSAL)


def _merge(*, ref: PrRef, expected_head_oid: str, query: CodeHostQuery | None = None) -> str:
    return "merged0sha"


def _gh_merge() -> BoundMergeResult:
    return GhPrApiClient().merge_pr_bound(slug=SLUG, pr_id=4861, expected_head_oid=HEAD)


def _glab_merge() -> BoundMergeResult:
    return GlabPrApiClient().merge_pr_bound(slug=SLUG, pr_id=4861, expected_head_oid=HEAD)


@pytest.fixture(autouse=True)
def _forge_account_stubs() -> Iterator[None]:
    with (
        patch("teatree.backends.github.client.GitHubCodeHost.current_user", return_value="merge-bot"),
        patch("teatree.backends.gitlab.client.GitLabCodeHost.current_user", return_value="merge-bot"),
    ):
        yield


class TestAuditBoundMerge(TestCase):
    def _assert_audited(self, merge: Callable[[], BoundMergeResult], host_kind: str) -> None:
        record_owned_pr_for_test(slug=SLUG, pr_id=4861, head_sha=HEAD, host_kind=host_kind)
        seed_merge_safe_verdict(slug=SLUG, pr_id=4861, sha=HEAD)
        with (
            patch.object(uncleared_mod, "execute_bound_merge", _merge),
            patch("teatree.core.merge.ci_rollup.CodeHostQuery.for_ref") as query,
        ):
            query.return_value.backend.current_user.return_value = "merge-bot"
            query.return_value.required_checks_status.return_value = "green"
            result = merge()
        assert result.merged is True
        assert MergeAudit.objects.filter(merged_sha="merged0sha", repo_slug=SLUG, clear__pr_id=4861).count() == 1

    def test_github_no_clear_route_records_merge_audit(self) -> None:
        self._assert_audited(_gh_merge, "github")

    def test_gitlab_no_clear_route_records_merge_audit(self) -> None:
        self._assert_audited(_glab_merge, "gitlab")


class TestGhBoundMerge:
    def test_refusal_carries_the_precondition_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(uncleared_mod, "execute_bound_merge", _refuse)

        result = _gh_merge()

        assert result.merged is False
        assert result.refusal == REFUSAL
        assert result.merged_sha == ""

    def test_success_has_no_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(uncleared_mod, "execute_bound_merge", _merge)

        assert _gh_merge() == BoundMergeResult(merged=True, merged_sha="merged0sha", refusal="")


class TestGlabBoundMerge:
    def test_refusal_carries_the_precondition_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(uncleared_mod, "execute_bound_merge", _refuse)

        result = _glab_merge()

        assert result.merged is False
        assert result.refusal == REFUSAL

    def test_success_has_no_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(uncleared_mod, "execute_bound_merge", _merge)

        assert _glab_merge() == BoundMergeResult(merged=True, merged_sha="merged0sha", refusal="")
