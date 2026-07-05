"""The ``read_score()`` seam over the recipe-weighted factory score (T4-PR-3)."""

from django.test import TestCase

from teatree.core.factory_score import FactoryScore
from teatree.loops.outer_loop.score import read_score


class TestReadScore(TestCase):
    def test_returns_a_factory_score(self) -> None:
        # An empty ledger yields an honest (RED, aggregate=None) score, never a
        # fabricated number — the read seam just surfaces it.
        score = read_score()
        assert isinstance(score, FactoryScore)
        assert score.verdict in {"ok", "regressing", "red"}
