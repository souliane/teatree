"""Instruction-compliance ledger for the dreaming accountant (#2663).

The root KPI is instruction compliance: the recurring complaint "you do NOT
follow instructions" is one failure mode wearing many masks — a rule was
PRESENT/AVAILABLE (a durable memory, a loaded-skill rule, a CLAUDE.md clause, a
system gate, or an explicit in-session user directive) and the agent acted
against it. Dream phase 3c mines each pass for those failures and persists them
here so the trend is auditable and the binding escalation rule is enforced.

Two rows are written per pass:

*   one :class:`InstructionComplianceSnapshot` summarising the pass
    (``instructions_observed`` / ``violations`` / ``compliance_rate`` /
    ``recurrences_count``), and
*   one :class:`InstructionComplianceRecord` per detected violation, carrying the
    rule source, the rule identity, the evidence excerpt, and whether the rule
    already had a durable memory (``is_recurrence``).

A recurrence — a rule that ALREADY had a durable memory and was violated again —
is the binding escalation trigger: its remediation MUST be a gate or an eval,
NEVER another memory. The persisted ``is_recurrence`` flag is what the
auto-escalation rule and the §4 gate (g) read to enforce that.

Mirrors the durable-ledger family already in core
(:class:`teatree.core.models.consolidated_memory.ConsolidatedMemory`,
:class:`teatree.core.models.dream_qa_probe.DreamQaProbe`): a TextChoices source
enum, model-owned factories, a custom manager.
"""

from typing import ClassVar

from django.db import models


class RuleSource(models.TextChoices):
    """Where the violated rule was PRESENT/AVAILABLE — the five root-cause origins.

    A rule a memory backs (``MEMORY``) is the one whose recurrence escalates to a
    gate/eval; the other origins still count as instruction-compliance failures
    but a first occurrence stays a candidate lesson.
    """

    MEMORY = "memory", "Durable memory"
    SKILL = "skill", "Loaded-skill rule"
    CLAUDE_MD = "claude_md", "CLAUDE.md clause"
    GATE = "gate", "System gate"
    IN_SESSION = "in_session", "Explicit in-session user directive"


class RemediationKind(models.TextChoices):
    """How a detected violation was remediated — the gate-(g) discriminator.

    A recurrence remediated with ``MEMORY`` is the forbidden non-fix the §4 gate
    (g) FAILS the pass on; ``ESCALATION`` (a filed gate/eval ticket) is the
    correct structural remediation. ``NONE`` is an as-yet-unremediated row.
    """

    NONE = "none", "Not yet remediated"
    MEMORY = "memory", "Wrote another memory (forbidden for a recurrence)"
    ESCALATION = "escalation", "Filed a gate/eval enforcement ticket"


class InstructionComplianceSnapshotManager(models.Manager["InstructionComplianceSnapshot"]):
    """Read surface for the compliance accountant + the `t3 dream compliance show` CLI."""

    def latest_for(self, overlay: str = "") -> "InstructionComplianceSnapshot | None":
        return self.filter(overlay=overlay).order_by("-created_at").first()


class InstructionComplianceSnapshot(models.Model):
    """One dream pass's instruction-compliance summary — the persisted metric.

    ``compliance_rate`` is ``(instructions_observed - violations) /
    instructions_observed`` (1.0 when nothing was observed). The snapshot is the
    headline `t3 dream compliance show` prints; the per-violation
    :class:`InstructionComplianceRecord` rows are its audit detail.
    """

    instructions_observed = models.PositiveIntegerField(default=0)
    violations = models.PositiveIntegerField(default=0)
    recurrences_count = models.PositiveIntegerField(default=0)
    compliance_rate = models.FloatField(default=1.0)
    overlay = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[InstructionComplianceSnapshotManager] = InstructionComplianceSnapshotManager()

    class Meta:
        db_table = "teatree_instruction_compliance_snapshot"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        return f"compliance-snapshot<{self.pk}:{self.compliance_rate:.2f}:{self.violations}v/{self.recurrences_count}r>"

    @staticmethod
    def compute_rate(*, instructions_observed: int, violations: int) -> float:
        """The compliance rate for a pass — 1.0 when nothing was observed."""
        if instructions_observed <= 0:
            return 1.0
        return (instructions_observed - violations) / instructions_observed

    @classmethod
    def record(
        cls,
        *,
        instructions_observed: int,
        violations: int,
        recurrences_count: int,
        overlay: str = "",
    ) -> "InstructionComplianceSnapshot":
        """Persist one pass's summary, computing ``compliance_rate`` from the counts."""
        return cls.objects.create(
            instructions_observed=instructions_observed,
            violations=violations,
            recurrences_count=recurrences_count,
            compliance_rate=cls.compute_rate(instructions_observed=instructions_observed, violations=violations),
            overlay=overlay,
        )


class InstructionComplianceRecordManager(models.Manager["InstructionComplianceRecord"]):
    """Read surface for the per-violation audit rows."""

    def open_escalations(self, overlay: str = "") -> "models.QuerySet[InstructionComplianceRecord]":
        """Recurrence rows already escalated to a filed gate/eval ticket."""
        return self.filter(
            overlay=overlay,
            is_recurrence=True,
            remediation=RemediationKind.ESCALATION,
        ).exclude(escalation_url="")


class InstructionComplianceRecord(models.Model):
    """One detected instruction-compliance violation — the audit row.

    ``rule_identity`` is the stable handle the auto-escalation rule dedups on (a
    memory slug, a skill-rule key, a CLAUDE.md clause id, a gate name, or a
    normalised in-session directive). ``is_recurrence`` is True when the rule
    already had a durable memory and was violated AGAIN — the binding escalation
    trigger whose only allowed remediation is a gate or an eval.
    """

    snapshot = models.ForeignKey(
        InstructionComplianceSnapshot,
        on_delete=models.CASCADE,
        related_name="records",
        null=True,
        blank=True,
    )
    rule_source = models.CharField(max_length=16, choices=RuleSource)
    rule_identity = models.CharField(max_length=512)
    evidence = models.TextField(blank=True, default="")
    is_recurrence = models.BooleanField(default=False)
    remediation = models.CharField(max_length=16, choices=RemediationKind, default=RemediationKind.NONE)
    escalation_url = models.CharField(max_length=512, blank=True, default="")
    overlay = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[InstructionComplianceRecordManager] = InstructionComplianceRecordManager()

    class Meta:
        db_table = "teatree_instruction_compliance_record"
        ordering: ClassVar = ["-created_at"]

    def __str__(self) -> str:
        recur = "recurrence" if self.is_recurrence else "first"
        return f"compliance-record<{self.pk}:{self.rule_source}:{recur}:{self.rule_identity[:40]}>"

    def mark_escalated(self, escalation_url: str) -> None:
        """Record that this recurrence was remediated by a filed gate/eval ticket."""
        self.remediation = RemediationKind.ESCALATION
        self.escalation_url = escalation_url.strip()
        self.save(update_fields=["remediation", "escalation_url"])

    def mark_remediated_with_memory(self) -> None:
        """Record the FORBIDDEN remediation: another memory for a recurrence.

        Only the §4 gate (g) test path stamps this — production never writes
        another memory for a recurrence, it escalates. The flag exists so the
        gate can FAIL a pass that took the forbidden path.
        """
        self.remediation = RemediationKind.MEMORY
        self.save(update_fields=["remediation"])
