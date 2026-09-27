"""Tests for the forge adapters' ``merge_pr_squash_bound`` — the real except-block, not a port fake."""

import pytest

import teatree.core.merge as merge_mod
import teatree.loop.scanners.pr_sweep_gitlab as gitlab_mod
from teatree.core.merge import MergePreconditionError
from teatree.loop.scanners.pr_sweep_adapters import GhPrApiClient
from teatree.loop.scanners.pr_sweep_gitlab import GlabPrApiClient
from teatree.loop.scanners.pr_sweep_types import BoundMergeResult
from teatree.utils.pr_ref import PrRef

SLUG = "souliane/teatree"
HEAD = "331eaa1e3f0f6a4e2d1b585ea1178c9baad6de95"
REFUSAL = "no rubric is recorded for this ticket"


def _refuse(*, ref: PrRef, expected_head_oid: str) -> str:
    raise MergePreconditionError(REFUSAL)


def _merge(*, ref: PrRef, expected_head_oid: str) -> str:
    return "merged0sha"


def _gh_merge() -> BoundMergeResult:
    return GhPrApiClient().merge_pr_squash_bound(slug=SLUG, pr_id=4861, expected_head_oid=HEAD)


def _glab_merge() -> BoundMergeResult:
    return GlabPrApiClient().merge_pr_squash_bound(slug=SLUG, pr_id=4861, expected_head_oid=HEAD)


class TestGhBoundMerge:
    def test_refusal_carries_the_precondition_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(merge_mod, "execute_bound_merge", _refuse)

        result = _gh_merge()

        assert result.merged is False
        assert result.refusal == REFUSAL
        assert result.merged_sha == ""

    def test_success_has_no_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(merge_mod, "execute_bound_merge", _merge)

        assert _gh_merge() == BoundMergeResult(merged=True, merged_sha="merged0sha", refusal="")


class TestGlabBoundMerge:
    def test_refusal_carries_the_precondition_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gitlab_mod, "execute_bound_merge", _refuse)

        result = _glab_merge()

        assert result.merged is False
        assert result.refusal == REFUSAL

    def test_success_has_no_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gitlab_mod, "execute_bound_merge", _merge)

        assert _glab_merge() == BoundMergeResult(merged=True, merged_sha="merged0sha", refusal="")
