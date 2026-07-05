"""Round-trip + delta-anchor tests for the FactoryScoreSnapshot ledger (SIG-PR-2)."""

import pytest
from django.test import TestCase

from teatree.core.factory_score import FactoryScore, ScoredSignal
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot


def _score(*, aggregate: float | None, recipe_sha: str, verdict: str = "ok") -> FactoryScore:
    return FactoryScore(
        aggregate=aggregate,
        verdict=verdict,
        coverage=1.0,
        coverage_floor=0.6,
        recipe_sha=recipe_sha,
        recipe_approved=True,
        window_days=28,
        signals=[
            ScoredSignal(
                provider_id="first_try_green",
                status="ok",
                value=0.9,
                normalized=0.9,
                weight=1.0,
                covered=True,
                red=False,
                verdict="ok",
            )
        ],
    )


class TestRoundTrip(TestCase):
    def test_record_snapshot_persists_provenance_and_reads_back(self) -> None:
        snap = FactoryScoreSnapshot.objects.record_snapshot(
            _score(aggregate=0.83, recipe_sha="abc123"),
            tree_sha="deadbeef",
            overlay="t3-teatree",
        )
        reloaded = FactoryScoreSnapshot.objects.get(pk=snap.pk)
        assert reloaded.aggregate == pytest.approx(0.83)
        assert reloaded.recipe_sha == "abc123"
        assert reloaded.tree_sha == "deadbeef"
        assert reloaded.overlay == "t3-teatree"
        assert reloaded.verdict == "ok"
        assert reloaded.coverage_floor == pytest.approx(0.6)
        assert reloaded.recipe_approved is True
        assert reloaded.signals[0]["provider_id"] == "first_try_green"

    def test_red_score_persists_null_aggregate_not_zero(self) -> None:
        snap = FactoryScoreSnapshot.objects.record_snapshot(
            _score(aggregate=None, recipe_sha="abc123", verdict="red"),
        )
        assert FactoryScoreSnapshot.objects.get(pk=snap.pk).aggregate is None


class TestDeltaAnchors(TestCase):
    def test_previous_is_scoped_to_overlay(self) -> None:
        FactoryScoreSnapshot.objects.record_snapshot(_score(aggregate=0.5, recipe_sha="r1"), overlay="a")
        latest_a = FactoryScoreSnapshot.objects.record_snapshot(_score(aggregate=0.6, recipe_sha="r1"), overlay="a")
        FactoryScoreSnapshot.objects.record_snapshot(_score(aggregate=0.9, recipe_sha="r1"), overlay="b")
        assert FactoryScoreSnapshot.objects.previous(overlay="a").pk == latest_a.pk

    def test_previous_is_none_when_empty(self) -> None:
        assert FactoryScoreSnapshot.objects.previous(overlay="a") is None

    def test_last_with_different_recipe_sha_skips_same_sha(self) -> None:
        old_recipe = FactoryScoreSnapshot.objects.record_snapshot(_score(aggregate=0.5, recipe_sha="r1"))
        FactoryScoreSnapshot.objects.record_snapshot(_score(aggregate=0.6, recipe_sha="r2"))
        FactoryScoreSnapshot.objects.record_snapshot(_score(aggregate=0.7, recipe_sha="r2"))
        # A regression cannot hide behind a re-weighting: the anchor is the last
        # snapshot under a DIFFERENT recipe, not the most recent same-recipe one.
        anchor = FactoryScoreSnapshot.objects.last_with_different_recipe_sha("r2")
        assert anchor.pk == old_recipe.pk

    def test_last_with_different_recipe_sha_none_when_all_same(self) -> None:
        FactoryScoreSnapshot.objects.record_snapshot(_score(aggregate=0.5, recipe_sha="r1"))
        assert FactoryScoreSnapshot.objects.last_with_different_recipe_sha("r1") is None
