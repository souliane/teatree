"""Append-only plan artifact — the DB record that gates plan() (BLUEPRINT §5.2).

``PlanArtifact`` is the PLANNED state's single source of truth: the only path
from STARTED to CODED passes through plan() → PLANNED → code() → CODED.
plan() is guarded by check_plan_artifact() which requires at least one
PlanArtifact row for the ticket.  No plan text in the DB → TransitionNotAllowed.

The model is intentionally append-only (no update/delete path).  The latest
artifact governs; storing previous versions preserves an immutable audit trail.
Mirrors the MergeClear/DbApproval pattern: a dedicated row alongside Ticket,
never a session-volatile JSON file.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.db_retry import retry_on_locked
from teatree.core.models.errors import NoPlanArtifactError  # noqa: F401 (re-exported for caller convenience)


class PlanArtifact(models.Model):
    """One immutable plan record authorising the STARTED → PLANNED transition.

    Written by the planner agent (via headless._record_success) or by the
    ``ticket plan`` management command.  The guarded factory
    (:meth:`record`) enforces a non-empty plan_text so a vacuous artifact
    cannot advance the FSM.
    """

    ticket = models.ForeignKey(
        "core.Ticket",
        on_delete=models.CASCADE,
        related_name="plan_artifacts",
    )
    plan_text = models.TextField()
    recorded_by = models.CharField(max_length=255, blank=True, default="")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_plan_artifact"
        ordering: ClassVar = ["-recorded_at"]

    def __str__(self) -> str:
        return f"plan-artifact<ticket:{self.ticket_id}@{self.recorded_at.isoformat()[:19]}>"  # type: ignore[attr-defined]  # Django FK accessor

    @classmethod
    def record(
        cls,
        *,
        ticket: "models.Model",
        plan_text: str,
        recorded_by: str,
    ) -> "PlanArtifact":
        """Guarded factory — the single path for creating a plan artifact.

        Validates that both plan_text and recorded_by are non-empty before
        writing any row. recorded_by is the author identity (the planning
        agent for auto-records, the human authorizer for an audited bypass);
        an anonymous artifact cannot advance the FSM. Raises ValueError on a
        blank/whitespace-only value so the call site gets a precise error
        rather than a vacuous or unattributable artifact. Construction is
        atomic so a rejected artifact leaves no partial row.
        """
        cleaned = plan_text.strip() if plan_text else ""
        if not cleaned:
            msg = "plan_text is required and must be non-empty"
            raise ValueError(msg)

        cleaned_author = recorded_by.strip() if recorded_by else ""
        if not cleaned_author:
            msg = "recorded_by is required and must be non-empty"
            raise ValueError(msg)

        def _create() -> "PlanArtifact":
            with transaction.atomic():
                return cls.objects.create(
                    ticket=ticket,
                    plan_text=cleaned,
                    recorded_by=cleaned_author,
                )

        return retry_on_locked(_create)
