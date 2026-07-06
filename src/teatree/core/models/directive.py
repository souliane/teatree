"""Durable ledger for one plain-language directive about teatree's own behavior (PR-6).

A :class:`Directive` is a first-class guarded-row FSM in the
``OuterLoopExperiment`` / ``MergeClear`` / ``DeferredQuestion`` family: it binds
the user's verbatim words to the typed :class:`MechanismSketch` an interpreter
produced and the human decisions that ratify it. Every state change is a guarded
helper that raises on an illegal transition; the row IS the audit history.

PR-6 implements the INTAKE arc — ``CAPTURED → CLARIFYING → INTERPRETED →
RATIFY_PENDING → ADMITTED`` — the directive-intake front-end of self-modification.
The post-``ADMITTED`` states (``IMPLEMENTING`` … ``REVERTED``) are declared so the
full FSM is coherent, but their driving transitions are the directive loop's
(a later PR); nothing in this module admits a directive past ``ADMITTED``.

The human is embedded structurally, not by convention: :meth:`admit` is the ONLY
writer of ``ADMITTED`` and RAISES unless a consumed (answered) ratify
:class:`~teatree.core.models.deferred_question.DeferredQuestion` exists, mirroring
``OuterLoopExperiment.admit`` — no code path can auto-admit a directive.

Ships inert: capture is explicit (the CLI) or, only when ``directive_loop_enabled``
is on, the ``DIRECTIVE``-intent router; at default config nothing writes a row, so
the migrated table stays empty (the ``ConfigSetting`` empty-table doctrine).
"""

from datetime import datetime
from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.factory_score_snapshot import FactoryScoreSnapshot
from teatree.core.models.incoming_event import IncomingEvent
from teatree.core.models.mechanism_sketch import MechanismSketch
from teatree.core.models.ticket import Ticket


class DirectiveError(ValueError):
    """Raised when a guarded :class:`Directive` transition is illegal."""


class DirectiveManager(models.Manager["Directive"]):
    def capture(
        self,
        raw_text: str,
        *,
        source: str,
        scope_overlay: str = "",
        source_event: "IncomingEvent | None" = None,
    ) -> "Directive":
        """The ONLY writer of the ``CAPTURED`` state — record one directive verbatim.

        Refuses blank text before any row is written, so a starved feeder can
        never seed an empty directive.
        """
        clean = raw_text.strip()
        if not clean:
            msg = "raw_text is required and must be non-empty"
            raise DirectiveError(msg)
        return self.create(
            raw_text=clean,
            source=source,
            scope_overlay=scope_overlay,
            source_event=source_event,
            state=Directive.State.CAPTURED,
        )

    def active(self) -> "models.QuerySet[Directive]":
        """Non-terminal directives (the working set the loop advances)."""
        return self.exclude(state__in=Directive.TERMINAL_STATES)


class Directive(models.Model):
    """One directive: raw text → typed sketch → human ratification → admission."""

    class State(models.TextChoices):
        CAPTURED = "captured", "Captured"
        CLARIFYING = "clarifying", "Clarifying"
        INTERPRETED = "interpreted", "Interpreted"
        RATIFY_PENDING = "ratify_pending", "Ratify pending"
        ADMITTED = "admitted", "Admitted"
        # Post-admission states — the directive loop's arc (a later PR). Declared
        # so the FSM is coherent; no transition in this module drives them.
        IMPLEMENTING = "implementing", "Implementing"
        CONFIGURING = "configuring", "Configuring"
        VERIFYING = "verifying", "Verifying"
        FULFILLED = "fulfilled", "Fulfilled"
        REJECTED = "rejected", "Rejected"
        REVERT_PENDING = "revert_pending", "Revert pending"
        REVERTED = "reverted", "Reverted"

    class Source(models.TextChoices):
        CLI = "cli", "CLI"
        INCOMING_EVENT = "incoming_event", "Incoming event"
        DREAM_ASK = "dream_ask", "Dream ask"

    TERMINAL_STATES: ClassVar[frozenset[str]] = frozenset(
        {str(State.FULFILLED.value), str(State.REJECTED.value), str(State.REVERTED.value)}
    )
    #: States from which a fresh interpretation may be recorded — the first
    #: interpret dispatch (``CAPTURED``) and a re-interpret after clarification
    #: (``CLARIFYING``).
    _INTERPRETABLE_STATES: ClassVar[frozenset[str]] = frozenset(
        {str(State.CAPTURED.value), str(State.CLARIFYING.value)}
    )

    created_at = models.DateTimeField(default=timezone.now)
    raw_text = models.TextField()
    source = models.CharField(max_length=32, choices=Source.choices)
    source_event = models.ForeignKey(
        IncomingEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="directives",
    )
    scope_overlay = models.CharField(max_length=64, blank=True, default="")
    constraint_statement = models.TextField(blank=True, default="")
    mechanism_sketch = models.JSONField(null=True, blank=True)
    generation = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=16, choices=State.choices, default=State.CAPTURED)
    ratify_question = models.ForeignKey(
        DeferredQuestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ratified_directives",
    )
    revert_question = models.ForeignKey(
        DeferredQuestion,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reverted_directives",
    )
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="directives",
    )
    baseline_snapshot = models.ForeignKey(
        FactoryScoreSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="baseline_of_directives",
    )
    post_snapshot = models.ForeignKey(
        FactoryScoreSnapshot,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="post_of_directives",
    )
    activation_applied_at = models.DateTimeField(null=True, blank=True)
    verify_started_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True, default="")
    extra = models.JSONField(default=dict, blank=True)

    objects: ClassVar[DirectiveManager] = DirectiveManager()

    class Meta:
        db_table = "teatree_directive"
        ordering: ClassVar = ["-created_at"]
        indexes: ClassVar = [
            models.Index(fields=["state", "created_at"], name="directive_state_created_idx"),
            models.Index(fields=["scope_overlay", "state"], name="directive_scope_state_idx"),
        ]

    def __str__(self) -> str:
        return f"directive<{self.pk}:{self.state} gen={self.generation}>"

    @property
    def is_terminal(self) -> bool:
        return self.state in self.TERMINAL_STATES

    @property
    def sketch(self) -> "MechanismSketch | None":
        """The typed :class:`MechanismSketch`, or ``None`` before interpretation."""
        if not isinstance(self.mechanism_sketch, dict):
            return None
        return MechanismSketch.from_dict(self.mechanism_sketch)

    def _require_state(self, *expected: "Directive.State") -> None:
        if self.state not in {e.value for e in expected}:
            allowed = ", ".join(repr(e.value) for e in expected)
            msg = f"illegal transition from {self.state!r}; expected one of [{allowed}]"
            raise DirectiveError(msg)

    def mark_clarifying(self) -> None:
        """``CAPTURED`` / ``CLARIFYING`` → ``CLARIFYING``: the interpreter needs answers.

        Idempotent from ``CLARIFYING`` so a re-interpretation that returns MORE
        questions parks again rather than raising.
        """
        self._require_state(self.State.CAPTURED, self.State.CLARIFYING)
        self.state = self.State.CLARIFYING
        self.save(update_fields=["state"])

    def record_interpretation(self, sketch: MechanismSketch, *, constraint_statement: str) -> None:
        """``CAPTURED`` / ``CLARIFYING`` → ``INTERPRETED``: bind the ratified-to-be sketch.

        The recorder writes the validated sketch here; the human ratifies it next.
        """
        if self.state not in self._INTERPRETABLE_STATES:
            self._require_state(self.State.CAPTURED, self.State.CLARIFYING)
        self.mechanism_sketch = sketch.to_dict()
        self.constraint_statement = constraint_statement.strip()
        self.state = self.State.INTERPRETED
        self.save(update_fields=["mechanism_sketch", "constraint_statement", "state"])

    def bump_generation(self) -> None:
        """Increment the interpretation generation before a re-dispatch (post-clarify)."""
        self.generation += 1
        self.save(update_fields=["generation"])

    def attach_ratification(self, question: DeferredQuestion) -> None:
        """``INTERPRETED`` → ``RATIFY_PENDING``: bind the human-approval question."""
        self._require_state(self.State.INTERPRETED)
        self.ratify_question = question
        self.state = self.State.RATIFY_PENDING
        self.save(update_fields=["ratify_question", "state"])

    def admit(self) -> None:
        """``RATIFY_PENDING`` → ``ADMITTED`` — the ONLY writer of the admitted state.

        RAISES unless :attr:`ratify_question` is a consumed (answered) row: no code
        path can admit a directive without a human's recorded decision, exactly as
        ``OuterLoopExperiment.admit`` gates the experiment.
        """
        self._require_state(self.State.RATIFY_PENDING)
        question = self.ratify_question
        if question is None or question.answered_at is None:
            msg = "cannot admit without a consumed (answered) ratify DeferredQuestion"
            raise DirectiveError(msg)
        self.state = self.State.ADMITTED
        self.save(update_fields=["state"])

    def reject(self, reason: str) -> None:
        """Any non-terminal state → ``REJECTED`` (ratify-denied / uninterpretable)."""
        if self.is_terminal:
            msg = f"cannot reject a terminal directive (state={self.state!r})"
            raise DirectiveError(msg)
        self.state = self.State.REJECTED
        self.decision_reason = reason
        self.save(update_fields=["state", "decision_reason"])

    def begin_implementation(self, ticket: Ticket, *, baseline_snapshot: "FactoryScoreSnapshot | None" = None) -> None:
        """``ADMITTED`` → ``IMPLEMENTING``: bind the synthetic mechanism ticket + admission baseline.

        The ``setting_policy_gate`` path — a real mechanism must be built. The
        baseline snapshot is the admission reference the VERIFYING no-collateral-
        regression evidence compares against, exactly like ``OuterLoopExperiment``.
        """
        self._require_state(self.State.ADMITTED)
        self.ticket = ticket
        fields = ["ticket", "state"]
        if baseline_snapshot is not None:
            self.baseline_snapshot = baseline_snapshot
            fields.append("baseline_snapshot")
        self.state = self.State.IMPLEMENTING
        self.save(update_fields=fields)

    def skip_to_configuring(self, *, baseline_snapshot: "FactoryScoreSnapshot | None" = None) -> None:
        """``ADMITTED`` → ``CONFIGURING``: the ``activation_only`` path — the mechanism already exists.

        A directive whose interpreter found an existing generic mechanism
        (``kind="activation_only"``) has nothing to build, so it skips
        ``IMPLEMENTING`` straight to the overlay-config write, still stamping the
        admission baseline the VERIFYING regression evidence needs.
        """
        self._require_state(self.State.ADMITTED)
        fields = ["state"]
        if baseline_snapshot is not None:
            self.baseline_snapshot = baseline_snapshot
            fields.append("baseline_snapshot")
        self.state = self.State.CONFIGURING
        self.save(update_fields=fields)

    def begin_configuring(self) -> None:
        """``IMPLEMENTING`` → ``CONFIGURING``: the mechanism ticket merged; apply activation next."""
        self._require_state(self.State.IMPLEMENTING)
        self.state = self.State.CONFIGURING
        self.save(update_fields=["state"])

    def begin_verifying(self, *, now: datetime | None = None) -> None:
        """``CONFIGURING`` → ``VERIFYING``: activation applied; start the verify horizon clock."""
        self._require_state(self.State.CONFIGURING)
        moment = now or timezone.now()
        self.activation_applied_at = moment
        self.verify_started_at = moment
        self.state = self.State.VERIFYING
        self.save(update_fields=["activation_applied_at", "verify_started_at", "state"])

    def record_fulfilled(self, *, reason: str, post_snapshot: "FactoryScoreSnapshot | None" = None) -> None:
        """``VERIFYING`` → ``FULFILLED``: all five evidence classes green."""
        self._require_state(self.State.VERIFYING)
        fields = ["decision_reason", "state"]
        if post_snapshot is not None:
            self.post_snapshot = post_snapshot
            fields.append("post_snapshot")
        self.decision_reason = reason
        self.state = self.State.FULFILLED
        self.save(update_fields=fields)

    def request_revert(self, *, reason: str, post_snapshot: "FactoryScoreSnapshot | None" = None) -> None:
        """``VERIFYING`` → ``REVERT_PENDING``: an evidence class failed; await a human revert."""
        self._require_state(self.State.VERIFYING)
        fields = ["decision_reason", "state"]
        if post_snapshot is not None:
            self.post_snapshot = post_snapshot
            fields.append("post_snapshot")
        self.decision_reason = reason
        self.state = self.State.REVERT_PENDING
        self.save(update_fields=fields)

    def attach_revert_question(self, question: DeferredQuestion) -> None:
        """Bind the human revert-approval question while in ``REVERT_PENDING``."""
        self._require_state(self.State.REVERT_PENDING)
        self.revert_question = question
        self.save(update_fields=["revert_question"])

    def record_reverted(self, *, revert_sha: str = "") -> None:
        """``REVERT_PENDING`` → ``REVERTED`` — gated on a consumed revert question.

        Revert is human-ratified, never automatic: RAISES unless
        :attr:`revert_question` is a consumed (answered) row, mirroring
        ``OuterLoopExperiment.record_reverted``. The overlay config is already rolled
        back the instant the directive entered ``REVERT_PENDING`` (the config write is
        reversible with no deploy); the human performs the code revert and closes out
        here. ``revert_sha`` is stamped into ``extra`` for provenance.
        """
        self._require_state(self.State.REVERT_PENDING)
        question = self.revert_question
        if question is None or question.answered_at is None:
            msg = "cannot revert without a consumed (answered) revert DeferredQuestion"
            raise DirectiveError(msg)
        fields = ["state"]
        if revert_sha:
            self.extra = {**(self.extra or {}), "revert_sha": revert_sha}
            fields.append("extra")
        self.state = self.State.REVERTED
        self.save(update_fields=fields)
