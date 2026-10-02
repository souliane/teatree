"""Durable ledger for the conversation-audit pass over captured sessions.

A :class:`SessionAuditRecord` is the persisted outcome of grading ONE captured
session against the ground-truth corpus: the categorical ``(outcome_axis,
expected_outcome, predicted_outcome)`` triple plus the :class:`EvalVerdict` the
grader produced. It is a sibling of :class:`teatree.core.models.EvalRunRecord`
(the metered-run ledger) — a parallel table rather than an overload of the
scenario-result row, because an audit grades a real session against an external
label, not a synthetic prompt against an inline assertion.

Privacy by construction: the record stores ONLY ids, indexes, slugs, and
categorical labels — never a tool input, a prompt, or a hook payload. The
``(expected_outcome, predicted_outcome)`` pairs the manager surfaces are the
confusion-matrix substrate the audit renderer (a later chunk) reads.
"""

from typing import ClassVar, TypedDict

from django.db import models
from django.utils import timezone

from teatree.core.models.eval_run import EvalRunRecord, EvalVerdict


class InvariantOutcome(TypedDict):
    """One conversation-invariant's outcome over an audited session.

    Mirrors :class:`teatree.eval.transcript_conformance.InvariantResult` as a
    JSON-serializable row stored in :attr:`SessionAuditRecord.invariant_results`.
    """

    invariant_id: str
    ok: bool
    offending_index: int | None


class SessionAuditQuerySet(models.QuerySet["SessionAuditRecord"]):
    def nominated(self) -> "SessionAuditQuerySet":
        return self.filter(nominated_for_label=True)

    def for_session(self, session_id: str) -> "SessionAuditQuerySet":
        return self.filter(session_id=session_id)

    def confusion_pairs(self, outcome_axis: str) -> list[tuple[str, str]]:
        """Return the ``(expected_outcome, predicted_outcome)`` pairs for one axis.

        The substrate a confusion matrix is built from: ONE pair per *session* on
        the axis — the most-recent audit of each session_id. Re-running the audit
        re-persists a session's row, so deduping to the latest keeps the matrix
        counting each session once (no ~Nx inflation after N runs) and reflects a
        changed re-audit verdict instead of blending stale and fresh pairs.
        """
        rows = self.filter(outcome_axis=outcome_axis).order_by("audited_at", "pk")
        latest: dict[str, tuple[str, str]] = {
            session_id: (expected, predicted)
            for session_id, expected, predicted in rows.values_list(
                "session_id", "expected_outcome", "predicted_outcome"
            )
        }
        return list(latest.values())


SessionAuditManager = models.Manager.from_queryset(SessionAuditQuerySet)


class SessionAuditRecord(models.Model):
    """One captured session's audit verdict against the ground-truth corpus."""

    audited_at = models.DateTimeField(default=timezone.now)
    session_id = models.CharField(max_length=128)
    corpus_entry_id = models.CharField(max_length=128)
    outcome_axis = models.CharField(max_length=64)
    expected_outcome = models.CharField(max_length=64)
    predicted_outcome = models.CharField(max_length=64)
    verdict = models.CharField(max_length=8, choices=EvalVerdict.choices)
    oracle = models.CharField(max_length=16)
    judge_rationale = models.CharField(max_length=512, blank=True, default="")
    invariant_results = models.JSONField(default=list, blank=True)
    gate_failure_slugs = models.JSONField(default=list, blank=True)
    nominated_for_label = models.BooleanField(default=False)
    eval_run = models.ForeignKey(
        EvalRunRecord,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_audits",
    )
    git_sha = models.CharField(max_length=64, blank=True, default="")

    objects = SessionAuditManager()

    class Meta:
        db_table = "teatree_session_audit"
        ordering: ClassVar = ["-audited_at"]
        indexes: ClassVar = [
            models.Index(fields=["session_id", "audited_at"], name="session_audit_session_idx"),
            models.Index(fields=["nominated_for_label", "audited_at"], name="session_audit_nominated_idx"),
        ]

    def __str__(self) -> str:
        return f"session-audit<{self.pk}:{self.session_id}:{self.corpus_entry_id}={self.verdict}>"

    @classmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record(  # noqa: PLR0913 — audit-ledger create API; each kwarg is a documented field.
        cls,
        *,
        session_id: str,
        corpus_entry_id: str,
        outcome_axis: str,
        expected_outcome: str,
        predicted_outcome: str,
        verdict: str,
        oracle: str,
        judge_rationale: str = "",
        invariant_results: list[InvariantOutcome] | None = None,
        gate_failure_slugs: list[str] | None = None,
        nominated_for_label: bool = False,
        eval_run: EvalRunRecord | None = None,
        git_sha: str = "",
    ) -> "SessionAuditRecord":
        return cls.objects.create(
            session_id=session_id,
            corpus_entry_id=corpus_entry_id,
            outcome_axis=outcome_axis,
            expected_outcome=expected_outcome,
            predicted_outcome=predicted_outcome,
            verdict=verdict,
            oracle=oracle,
            judge_rationale=judge_rationale,
            invariant_results=invariant_results or [],
            gate_failure_slugs=gate_failure_slugs or [],
            nominated_for_label=nominated_for_label,
            eval_run=eval_run,
            git_sha=git_sha,
        )
