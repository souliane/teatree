"""Append-only plan artifact — the DB record that gates plan() (BLUEPRINT §5.2).

``PlanArtifact`` is the PLANNED state's single source of truth: the only path
from STARTED to CODED passes through plan() → PLANNED → code() → CODED.
plan() is guarded by check_plan_artifact() which requires at least one
PlanArtifact row for the ticket.  No plan text in the DB → TransitionNotAllowed.

The model is intentionally append-only (no update/delete path).  The latest
artifact governs; storing previous versions preserves an immutable audit trail.
Mirrors the MergeClear/DbApproval pattern: a dedicated row alongside Ticket,
never a session-volatile JSON file.

SELFCATCH-3 hardened the vacuity hole: a row now carries the ``base_sha`` the
plan was authored against and a four-section ``adequacy`` manifest. Under
``require_plan_adequacy`` (opt-in), :meth:`record` refuses a new row without a
40-char base SHA and a complete manifest — so a scope+acceptance thin spec can no
longer pass as a plan, and a plan bound to a stale base is detectable downstream.
"""

from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.modelkit.db_retry import retry_on_locked
from teatree.core.models.errors import NoPlanArtifactError  # noqa: F401 (re-exported for caller convenience)
from teatree.core.models.plan_adequacy import all_negated_adequacy, is_adequate, is_valid_base_sha
from teatree.core.models.types import PlanAdequacy


def plan_adequacy_required(overlay_name: str | None = None) -> bool:
    """Whether the plan-adequacy/currency gate is in force for *overlay_name* (overlay → global)."""
    return bool(get_effective_settings(overlay_name).require_plan_adequacy)


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
    # SELFCATCH-3 late-bound-plan: the target-branch HEAD the plan was authored
    # against. Blank on legacy rows (pre-migration) — treated as stale under the
    # flag (fail-safe). New rows under the flag require a full 40-char hex SHA.
    base_sha = models.CharField(max_length=64, blank=True, default="")
    # SELFCATCH-3 plan-adequacy: the four-section manifest (design,
    # integration_seams, edge_cases, test_strategy). Empty on legacy rows.
    adequacy = models.JSONField(default=dict, blank=True)

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
        base_sha: str = "",
        adequacy: PlanAdequacy | dict | None = None,
    ) -> "PlanArtifact":
        """Guarded factory — the enforced path for creating a plan artifact.

        Validates that both plan_text and recorded_by are non-empty before
        writing any row. recorded_by is the author identity (the planning
        agent for auto-records, the human authorizer for an audited bypass);
        an anonymous artifact cannot advance the FSM. Raises ValueError on a
        blank/whitespace-only value so the call site gets a precise error
        rather than a vacuous or unattributable artifact. Construction is
        atomic so a rejected artifact leaves no partial row.

        SELFCATCH-3: when ``require_plan_adequacy`` is on for the ticket's
        overlay, a new row additionally requires a 40-char hex ``base_sha`` and a
        complete four-section ``adequacy`` manifest. A scope+acceptance-only thin
        spec — no seams/edge-cases/test-strategy claims — is refused here, before
        any row is written. The audited-bypass carve-out is the sibling
        :meth:`record_bypass`, exempt from this enforcement.
        """
        cleaned_text, cleaned_author = _clean_required(plan_text, recorded_by)
        cleaned_sha = base_sha.strip() if base_sha else ""
        manifest: dict = dict(adequacy) if adequacy else {}
        if plan_adequacy_required(getattr(ticket, "overlay", "") or None):
            _require_adequate_bound_plan(cleaned_sha, manifest)
        return cls._create_row(ticket, cleaned_text, cleaned_author, cleaned_sha, manifest)

    @classmethod
    def record_bypass(cls, *, ticket: "models.Model", plan_text: str, recorded_by: str) -> "PlanArtifact":
        """Audited-bypass factory — records a plan EXEMPT from adequacy enforcement.

        The sibling of :meth:`record` for the human-authorized ``plan-bypass`` and
        the retroactive reconcile. It writes an all-negatives manifest (a
        ``no_seams`` plan) so the row is structurally adequate and carries no seams
        for the currency gate to guard — ``plan()`` advances end-to-end even under
        the strict flag, without a special case in the gate. A bypassed plan has no
        ``base_sha`` bind, so under the flag it still needs ``plan-reaffirm`` to
        reach CODED. A purpose-typed method, not a flag on :meth:`record`, so the
        exemption is explicit at the call site.
        """
        cleaned_text, cleaned_author = _clean_required(plan_text, recorded_by)
        manifest = dict(all_negated_adequacy(cleaned_text))
        return cls._create_row(ticket, cleaned_text, cleaned_author, "", manifest)

    @classmethod
    def _create_row(
        cls, ticket: "models.Model", plan_text: str, recorded_by: str, base_sha: str, adequacy: dict
    ) -> "PlanArtifact":
        """Atomic, lock-retried row write shared by :meth:`record` and :meth:`record_bypass`."""

        def _create() -> "PlanArtifact":
            with transaction.atomic():
                return cls.objects.create(
                    ticket=ticket,
                    plan_text=plan_text,
                    recorded_by=recorded_by,
                    base_sha=base_sha,
                    adequacy=adequacy,
                )

        return retry_on_locked(_create)


def _clean_required(plan_text: str, recorded_by: str) -> tuple[str, str]:
    """Strip-and-require ``plan_text`` + ``recorded_by``; raise ValueError on a blank."""
    cleaned_text = plan_text.strip() if plan_text else ""
    if not cleaned_text:
        msg = "plan_text is required and must be non-empty"
        raise ValueError(msg)
    cleaned_author = recorded_by.strip() if recorded_by else ""
    if not cleaned_author:
        msg = "recorded_by is required and must be non-empty"
        raise ValueError(msg)
    return cleaned_text, cleaned_author


def _require_adequate_bound_plan(base_sha: str, adequacy: dict) -> None:
    """Refuse a thin/unbound plan under ``require_plan_adequacy`` (raises ValueError)."""
    if not is_valid_base_sha(base_sha):
        msg = (
            "require_plan_adequacy: a plan needs base_sha = the 40-char hex target-branch "
            "HEAD it was authored against (got "
            f"{base_sha[:12]!r}). Pass it so the plan can be bound to the base it planned "
            "against and detected as stale when the base moves."
        )
        raise ValueError(msg)
    if not is_adequate(adequacy):
        msg = (
            "require_plan_adequacy: a plan needs a complete four-section adequacy manifest "
            "(design, integration_seams, edge_cases, test_strategy); each section must be "
            "substantive OR carry an explicit reasoned negative (e.g. no_seams: <reason>). A "
            "scope+acceptance-only thin spec has no seams/edge-cases/test-strategy claims and "
            "is refused — write a real plan or record explicit negatives."
        )
        raise ValueError(msg)
