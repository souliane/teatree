"""Durable ledger for one T4 autoresearch experiment (T4-PR-3).

An :class:`OuterLoopExperiment` binds a hypothesis to the git artifacts and the
human decisions that resolve it: the admission baseline + post-horizon
:class:`~teatree.core.models.factory_score_snapshot.FactoryScoreSnapshot`, the
ratify + revert :class:`~teatree.core.models.deferred_question.DeferredQuestion`
gates, the synthetic implement ``Ticket``, and the kept/reverted git shas. The
row IS the audit history — every state change is a guarded helper that raises on
an illegal transition, mirroring the ``MergeClear`` / ``DeferredQuestion``
durable-row family.

The human is embedded structurally, not by convention: :meth:`admit` is the ONLY
writer of the ``ADMITTED`` state and RAISES unless a consumed (answered) ratify
question exists, so no code path can auto-admit an experiment; :meth:`record_reverted`
is likewise gated on a consumed revert question. The keep/revert decision is taken
by the pure rule in :mod:`teatree.loops.outer_loop.decide` — an experiment whose
score does not improve is never KEPT.

Ships inert: the outer loop that writes these rows refuses every tick at default
config (flag off, loop row disabled, critic not live, signals untrusted), so the
migrated table stays empty — the only persistent footprint (the ``ConfigSetting``
empty-table doctrine).
"""

import datetime as dt
from dataclasses import dataclass
from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot
from teatree.core.models.ticket import Ticket


class OuterLoopExperimentError(ValueError):
    """Raised when a guarded :class:`OuterLoopExperiment` transition is illegal."""


@dataclass(frozen=True, slots=True)
class ProposalSpec:
    """What one experiment proposes — the cohesive input the PROPOSE writer takes.

    Bundling the four proposal fields keeps :meth:`OuterLoopExperimentManager.propose`
    a small factory (the proposer builds one of these; the manager binds it plus the
    scope/baseline context).
    """

    hypothesis: str
    target_provider_id: str
    source: str
    regress_band: float = 0.0


class OuterLoopExperimentManager(models.Manager["OuterLoopExperiment"]):
    def propose(
        self,
        spec: ProposalSpec,
        *,
        overlay: str = "",
        baseline_snapshot: "FactoryScoreSnapshot | None" = None,
    ) -> "OuterLoopExperiment":
        """The only PROPOSE writer — create one experiment in the ``PROPOSED`` state.

        Rejects a blank hypothesis before any row is written; a starved proposer
        can never seed an empty experiment.
        """
        clean = spec.hypothesis.strip()
        if not clean:
            msg = "hypothesis is required and must be non-empty"
            raise OuterLoopExperimentError(msg)
        return self.create(
            hypothesis=clean,
            target_provider_id=spec.target_provider_id,
            source=spec.source,
            overlay=overlay,
            regress_band=spec.regress_band,
            baseline_snapshot=baseline_snapshot,
            state=OuterLoopExperiment.State.PROPOSED,
        )

    def active(self, *, overlay: str = "") -> "models.QuerySet[OuterLoopExperiment]":
        """Non-terminal experiments in *overlay*'s scope (the max-concurrent set)."""
        return self.filter(overlay=overlay).exclude(state__in=OuterLoopExperiment.TERMINAL_STATES)

    def active_count(self, *, overlay: str = "") -> int:
        return self.active(overlay=overlay).count()

    def weekly_count(self, *, overlay: str = "", now: "dt.datetime | None" = None) -> int:
        """Experiments proposed in the trailing 7 days (the weekly-cap window)."""
        cutoff = (now or timezone.now()) - dt.timedelta(days=7)
        return self.filter(overlay=overlay, created_at__gte=cutoff).count()

    def consecutive_non_kept(self, *, overlay: str = "") -> int:
        """Trailing terminal decisions that are NOT ``KEPT``, up to the first ``KEPT``.

        The convergence-brake counter: three in a row parks the loop instead of a
        fourth proposal, so a broken proposer cannot churn forever.
        """
        recent = self.filter(overlay=overlay, state__in=OuterLoopExperiment.TERMINAL_STATES).order_by(
            "-created_at", "-pk"
        )
        count = 0
        for experiment in recent:
            if experiment.state == OuterLoopExperiment.State.KEPT:
                break
            count += 1
        return count


class OuterLoopExperiment(models.Model):
    """One autoresearch experiment: hypothesis → shas → score snapshots → decisions."""

    class State(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        RATIFY_PENDING = "ratify_pending", "Ratify pending"
        ADMITTED = "admitted", "Admitted"
        IMPLEMENTING = "implementing", "Implementing"
        MEASURING = "measuring", "Measuring"
        KEPT = "kept", "Kept"
        REVERT_PENDING = "revert_pending", "Revert pending"
        REVERTED = "reverted", "Reverted"
        REJECTED = "rejected", "Rejected"

    class Source(models.TextChoices):
        SIGNAL_REGRESSION = "signal_regression", "Signal regression"
        CORE_GAP = "core_gap", "Consolidated-memory core gap"
        OPERATOR = "operator", "Operator hypothesis"

    class Decision(models.TextChoices):
        KEEP = "keep", "Keep"
        REVERT = "revert", "Revert"
        REJECT = "reject", "Reject"

    TERMINAL_STATES: ClassVar[frozenset[str]] = frozenset(
        {str(State.KEPT.value), str(State.REVERTED.value), str(State.REJECTED.value)}
    )

    created_at = models.DateTimeField(default=timezone.now)
    overlay = models.CharField(max_length=64, blank=True, default="")
    hypothesis = models.TextField()
    source = models.CharField(max_length=32, choices=Source.choices)
    target_provider_id = models.CharField(max_length=64, blank=True, default="")
    regress_band = models.FloatField(default=0.0)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PROPOSED)
    baseline_snapshot = models.ForeignKey(
        FactoryScoreSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="baseline_of_experiments",
    )
    post_snapshot = models.ForeignKey(
        FactoryScoreSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="post_of_experiments",
    )
    ratify_question = models.ForeignKey(
        DeferredQuestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ratified_experiments",
    )
    revert_question = models.ForeignKey(
        DeferredQuestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reverted_experiments",
    )
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="outer_loop_experiments",
    )
    merged_sha = models.CharField(max_length=64, blank=True, default="")
    revert_sha = models.CharField(max_length=64, blank=True, default="")
    decision = models.CharField(max_length=8, choices=Decision.choices, blank=True, default="")
    decision_reason = models.TextField(blank=True, default="")
    measure_started_at = models.DateTimeField(null=True, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    objects: ClassVar[OuterLoopExperimentManager] = OuterLoopExperimentManager()

    class Meta:
        db_table = "teatree_outer_loop_experiment"
        ordering: ClassVar = ["-created_at"]
        indexes: ClassVar = [
            models.Index(fields=["overlay", "state"], name="ole_overlay_state_idx"),
            models.Index(fields=["overlay", "created_at"], name="ole_overlay_created_idx"),
        ]

    def __str__(self) -> str:
        return f"outer-loop-experiment<{self.pk}:{self.state} target={self.target_provider_id}>"

    @property
    def is_terminal(self) -> bool:
        return self.state in self.TERMINAL_STATES

    def _require_state(self, expected: "OuterLoopExperiment.State") -> None:
        if self.state != expected.value:
            msg = f"illegal transition from {self.state!r}; expected {expected.value!r}"
            raise OuterLoopExperimentError(msg)

    def attach_ratification(self, question: DeferredQuestion) -> None:
        """``PROPOSED`` → ``RATIFY_PENDING``: bind the human-approval question."""
        self._require_state(self.State.PROPOSED)
        self.ratify_question = question
        self.state = self.State.RATIFY_PENDING
        self.save(update_fields=["ratify_question", "state"])

    def admit(self) -> None:
        """``RATIFY_PENDING`` → ``ADMITTED`` — the ONLY writer of the admitted state.

        RAISES unless :attr:`ratify_question` is a consumed (answered) row: no code
        path can admit an experiment without a human's recorded decision.
        """
        self._require_state(self.State.RATIFY_PENDING)
        question = self.ratify_question
        if question is None or question.answered_at is None:
            msg = "cannot admit without a consumed (answered) ratify DeferredQuestion"
            raise OuterLoopExperimentError(msg)
        self.state = self.State.ADMITTED
        self.save(update_fields=["state"])

    def reject(self, reason: str) -> None:
        """Any non-terminal state → ``REJECTED`` (ratify-denied / converged)."""
        if self.is_terminal:
            msg = f"cannot reject a terminal experiment (state={self.state!r})"
            raise OuterLoopExperimentError(msg)
        self.state = self.State.REJECTED
        self.decision = self.Decision.REJECT
        self.decision_reason = reason
        self.save(update_fields=["state", "decision", "decision_reason"])

    def begin_implementation(self, ticket: Ticket) -> None:
        """``ADMITTED`` → ``IMPLEMENTING``: bind the synthetic maker ticket."""
        self._require_state(self.State.ADMITTED)
        self.ticket = ticket
        self.state = self.State.IMPLEMENTING
        self.save(update_fields=["ticket", "state"])

    def arm_measure(self, *, now: "dt.datetime | None" = None) -> None:
        """``IMPLEMENTING`` → ``MEASURING``: start the post-merge horizon clock."""
        self._require_state(self.State.IMPLEMENTING)
        self.measure_started_at = now or timezone.now()
        self.state = self.State.MEASURING
        self.save(update_fields=["measure_started_at", "state"])

    def record_kept(self, *, post_snapshot: FactoryScoreSnapshot, merged_sha: str, reason: str) -> None:
        """``MEASURING`` → ``KEPT``: bind the post score + the kept git sha."""
        self._require_state(self.State.MEASURING)
        self.post_snapshot = post_snapshot
        self.merged_sha = merged_sha
        self.decision = self.Decision.KEEP
        self.decision_reason = reason
        self.state = self.State.KEPT
        self.save(update_fields=["post_snapshot", "merged_sha", "decision", "decision_reason", "state"])

    def request_revert(self, *, post_snapshot: FactoryScoreSnapshot, reason: str) -> None:
        """``MEASURING`` → ``REVERT_PENDING``: a non-improving experiment awaits human revert."""
        self._require_state(self.State.MEASURING)
        self.post_snapshot = post_snapshot
        self.decision = self.Decision.REVERT
        self.decision_reason = reason
        self.state = self.State.REVERT_PENDING
        self.save(update_fields=["post_snapshot", "decision", "decision_reason", "state"])

    def attach_revert_question(self, question: DeferredQuestion) -> None:
        """Bind the human revert-approval question while in ``REVERT_PENDING``."""
        self._require_state(self.State.REVERT_PENDING)
        self.revert_question = question
        self.save(update_fields=["revert_question"])

    def record_reverted(self, *, revert_sha: str) -> None:
        """``REVERT_PENDING`` → ``REVERTED`` — gated on a consumed revert question.

        Revert is human-ratified, never automatic: RAISES unless
        :attr:`revert_question` is a consumed (answered) row.
        """
        self._require_state(self.State.REVERT_PENDING)
        question = self.revert_question
        if question is None or question.answered_at is None:
            msg = "cannot revert without a consumed (answered) revert DeferredQuestion"
            raise OuterLoopExperimentError(msg)
        self.revert_sha = revert_sha
        self.state = self.State.REVERTED
        self.save(update_fields=["revert_sha", "state"])
