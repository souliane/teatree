"""Append-only ledger of recipe-weighted factory scores (SIG-PR-2).

Every scored read the outer loop persists is one :class:`FactoryScoreSnapshot`
carrying the aggregate, its verdict, and the two provenance keys that make a
regression impossible to hide: ``recipe_sha`` (which recipe produced it) and
``tree_sha`` (the code-under-test commit). The fat manager gives the outer loop
its time-series diffs — :meth:`previous` (the last snapshot) and
:meth:`last_with_different_recipe_sha` (the last snapshot under a *different*
recipe), so re-weighting the recipe cannot mask a real regression behind its own
sha.

Flag-gated OFF is the shipped state: with ``factory_score_enabled`` false NO row
is ever written (``t3 <overlay> recipe score --record`` refuses), so the migrated table
stays empty — the only persistent footprint, matching the ``ConfigSetting``
empty-table doctrine.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.factory_score import FactoryScore


class FactoryScoreSnapshotManager(models.Manager["FactoryScoreSnapshot"]):
    def record_snapshot(
        self,
        score: "FactoryScore",
        *,
        tree_sha: str = "",
        overlay: str = "",
    ) -> "FactoryScoreSnapshot":
        """Persist one scored read as a snapshot row (the only writer).

        Stamps both provenance keys — ``score.recipe_sha`` and the caller-supplied
        ``tree_sha`` (the HEAD the score was computed against) — plus the deltas
        already resolved on the score, so a snapshot is a self-contained audit
        record binding aggregate → recipe → tree.
        """
        return self.create(
            overlay=overlay,
            window_days=score.window_days,
            recipe_sha=score.recipe_sha,
            tree_sha=tree_sha,
            aggregate=score.aggregate,
            verdict=score.verdict,
            coverage=score.coverage,
            coverage_floor=score.coverage_floor,
            recipe_approved=score.recipe_approved,
            signals=[sig.to_dict() for sig in score.signals],
            delta_vs_previous=score.delta_vs_previous,
            delta_vs_last_different_recipe_sha=score.delta_vs_last_different_recipe_sha,
        )

    def previous(self, *, overlay: str = "") -> "FactoryScoreSnapshot | None":
        """The most recent snapshot in *overlay*'s scope, or ``None``."""
        return self.filter(overlay=overlay).order_by("-created_at", "-pk").first()

    def last_with_different_recipe_sha(self, recipe_sha: str, *, overlay: str = "") -> "FactoryScoreSnapshot | None":
        """The most recent snapshot whose ``recipe_sha`` differs from *recipe_sha*.

        The cross-recipe delta anchor: comparing today's score against the last
        score under a *different* recipe is what prevents a re-weighting from
        hiding a regression behind its own new sha.
        """
        return self.filter(overlay=overlay).exclude(recipe_sha=recipe_sha).order_by("-created_at", "-pk").first()


class FactoryScoreSnapshot(models.Model):
    """One recorded recipe-weighted factory score, with recipe + tree provenance."""

    created_at = models.DateTimeField(default=timezone.now)
    overlay = models.CharField(max_length=64, blank=True, default="")
    window_days = models.IntegerField(default=0)
    recipe_sha = models.CharField(max_length=64)
    tree_sha = models.CharField(max_length=64, blank=True, default="")
    # NULL when the score is untrustworthy (RED): an untrustworthy score is never
    # a number, so its persisted aggregate is NULL, distinct from a real 0.0.
    aggregate = models.FloatField(null=True, blank=True, default=None)
    verdict = models.CharField(max_length=16)
    coverage = models.FloatField(default=0.0)
    coverage_floor = models.FloatField(default=0.0)
    recipe_approved = models.BooleanField(default=False)
    signals = models.JSONField(default=list, blank=True)
    delta_vs_previous = models.FloatField(null=True, blank=True, default=None)
    delta_vs_last_different_recipe_sha = models.FloatField(null=True, blank=True, default=None)

    objects: ClassVar[FactoryScoreSnapshotManager] = FactoryScoreSnapshotManager()

    class Meta:
        db_table = "teatree_factory_score_snapshot"
        ordering: ClassVar = ["-created_at"]
        indexes: ClassVar = [
            models.Index(fields=["overlay", "created_at"], name="fss_overlay_created_idx"),
            models.Index(fields=["recipe_sha", "created_at"], name="fss_recipe_created_idx"),
        ]

    def __str__(self) -> str:
        agg = "None" if self.aggregate is None else f"{self.aggregate:.3f}"
        return f"factory-score<{self.pk}:{self.verdict} agg={agg} recipe={self.recipe_sha[:8]}>"
