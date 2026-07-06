"""Reconstruct a FactoryScore from a persisted snapshot row (north-star PR-7)."""

import pytest
from django.test import TestCase

from teatree.core.models import FactoryScoreSnapshot
from teatree.loops.shared.score_snapshot import snapshot_to_score


class TestSnapshotToScore(TestCase):
    def test_round_trips_the_signals_and_provenance(self) -> None:
        snapshot = FactoryScoreSnapshot.objects.create(
            overlay="t3-teatree",
            window_days=7,
            recipe_sha="abc",
            aggregate=0.8,
            verdict="ok",
            coverage=0.95,
            coverage_floor=0.9,
            recipe_approved=True,
            signals=[
                {
                    "provider_id": "review_catch",
                    "status": "ok",
                    "value": 0.9,
                    "normalized": 0.9,
                    "weight": 1.0,
                    "covered": True,
                    "red": False,
                    "verdict": "ok",
                }
            ],
        )
        score = snapshot_to_score(snapshot)
        assert score.aggregate == pytest.approx(0.8)
        assert score.verdict == "ok"
        assert score.recipe_sha == "abc"
        assert len(score.signals) == 1
        assert score.signals[0].provider_id == "review_catch"
        assert score.signals[0].verdict == "ok"

    def test_tolerates_a_sparse_signal_dict(self) -> None:
        snapshot = FactoryScoreSnapshot.objects.create(
            overlay="",
            window_days=7,
            recipe_sha="s",
            aggregate=None,
            verdict="red",
            coverage=0.0,
            coverage_floor=0.9,
            signals=[{"provider_id": "x", "status": "instrumentation_gap"}],
        )
        score = snapshot_to_score(snapshot)
        assert score.aggregate is None
        assert score.signals[0].weight == pytest.approx(0.0)
        assert score.signals[0].covered is False
