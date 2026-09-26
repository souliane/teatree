"""Append-only plan artifact — the DB record that gates plan() (BLUEPRINT §5.2).

``PlanArtifact`` is the PLANNED state's single source of truth: the only path
from STARTED to CODED passes through plan() → PLANNED → code() → CODED.
plan() is guarded by check_plan_artifact() which requires at least one
PlanArtifact row for the ticket.  No plan text in the DB → TransitionNotAllowed.

The model is intentionally append-only (no update/delete path).  The latest
artifact governs; storing previous versions preserves an immutable audit trail.
Mirrors the MergeClear/DbApproval pattern: a dedicated row alongside Ticket,
never a session-volatile JSON file.

SELFCATCH-3 hardened the vacuity hole: a row carries the ``base_sha`` the plan was
authored against and a five-section ``adequacy`` manifest, and :meth:`record` refuses
a new row without a 40-char base SHA and a complete manifest — so a scope+acceptance
thin spec cannot pass as a plan, and a plan bound to a stale base is detectable
downstream. There is no setting that relaxes this; the audited escapes are
:meth:`record_bypass` (the human-authorized plan-bypass, and the only writer of the
all-negatives manifest the rubric gate reads as a waiver) and ``skip-planning``.

The fifth section makes the plan a PRODUCER as well as a record: its
``acceptance_criteria`` become the ticket's ``core.Rubric`` rows in the same atomic,
so the acceptance-criteria list has exactly one home and arming the done-gate can
never mean arming it against an empty table. A ``none_reason`` there is an ordinary
reasoned negative — it writes no rows and waives nothing.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.db_retry import retry_on_locked
from teatree.core.models.errors import NoPlanArtifactError  # noqa: F401 (re-exported for caller convenience)
from teatree.core.models.plan_adequacy import (
    all_negated_adequacy,
    declared_acceptance_criteria,
    is_adequate,
    is_plan_bypass_shaped,
    is_valid_base_sha,
)
from teatree.core.models.rubric import Rubric
from teatree.core.models.ticket import Ticket
from teatree.core.models.types import PlanAdequacy


class PlanArtifact(models.Model):
    """One immutable plan record authorising the STARTED → PLANNED transition.

    Written by the planner agent (via headless._record_success) or by the
    ``ticket plan`` management command.  The guarded factory
    (:meth:`record`) enforces a non-empty plan_text so a vacuous artifact
    cannot advance the FSM.
    """

    ticket = models.ForeignKey(
        Ticket,
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
    # SELFCATCH-3 plan-adequacy: the five-section manifest (design, integration_seams,
    # edge_cases, test_strategy, acceptance_criteria). Empty on legacy rows.
    adequacy = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "teatree_plan_artifact"
        ordering: ClassVar = ["-recorded_at"]

    if TYPE_CHECKING:
        # Django synthesises the ``<fk>_id`` shadow attribute at class-prep time —
        # invisible to a static checker. Declared here (annotation-only, never
        # evaluated at runtime) so ``__str__`` reads the id without a relation query.
        ticket_id: int

    def __str__(self) -> str:
        return f"plan-artifact<ticket:{self.ticket_id}@{self.recorded_at.isoformat()[:19]}>"

    @classmethod
    def record(
        cls,
        *,
        ticket: Ticket,
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

        A new row requires a 40-char hex ``base_sha`` and a complete five-section
        ``adequacy`` manifest, unconditionally. A scope+acceptance-only thin spec — no
        seams/edge-cases/test-strategy claims — is refused here, before any row is
        written, and so is a manifest that declares nothing at all: that shape waives the
        rubric gate, so only the audited :meth:`record_bypass` may write it.

        The manifest's ``acceptance_criteria`` are added to the ticket's rubric in the
        same atomic, so a refused rubric (one satisfiable by inaction) rolls the plan
        back with it — the plan and the criteria it declares land together or not at all.
        """
        cleaned_text, cleaned_author = _clean_required(plan_text, recorded_by)
        cleaned_sha = base_sha.strip() if base_sha else ""
        manifest: dict = dict(adequacy) if adequacy else {}
        _require_adequate_bound_plan(cleaned_sha, manifest)
        return cls._create_row(ticket, cleaned_text, cleaned_author, cleaned_sha, manifest)

    @classmethod
    def record_bypass(cls, *, ticket: Ticket, plan_text: str, recorded_by: str) -> "PlanArtifact":
        """Audited-bypass factory — records a plan EXEMPT from adequacy enforcement.

        The sibling of :meth:`record` for the human-authorized ``plan-bypass`` and
        the retroactive reconcile, and the ONLY writer of the all-negatives manifest
        (:func:`all_negated_adequacy`) — :meth:`record` refuses that shape. The manifest
        is therefore the durable proof a human authorized the bypass, which is what the
        rubric gate reads as its one waiver signal.
        Because it has no declared seams, the currency gate finds nothing to guard —
        ``check_plan_current`` passes it DIRECTLY (a ``no_seams`` plan can never be
        stale on a seam), so a bypassed plan reaches CODED with no ``plan-reaffirm``
        step even under the strict flag. A purpose-typed method, not a flag on
        :meth:`record`, so the exemption is explicit at the call site.
        """
        cleaned_text, cleaned_author = _clean_required(plan_text, recorded_by)
        manifest = dict(all_negated_adequacy(cleaned_text))
        return cls._create_row(ticket, cleaned_text, cleaned_author, "", manifest)

    @classmethod
    def _create_row(
        cls, ticket: Ticket, plan_text: str, recorded_by: str, base_sha: str, adequacy: dict
    ) -> "PlanArtifact":
        """Atomic, lock-retried row write shared by :meth:`record` and :meth:`record_bypass`."""

        def _create() -> "PlanArtifact":
            with transaction.atomic():
                artifact = cls.objects.create(
                    ticket=ticket,
                    plan_text=plan_text,
                    recorded_by=recorded_by,
                    base_sha=base_sha,
                    adequacy=adequacy,
                )
                Rubric.add_criteria(ticket, list(declared_acceptance_criteria(adequacy)))
                return artifact

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
    """Refuse a thin/unbound plan, or one shaped like the human-authorized bypass (raises ValueError)."""
    if is_plan_bypass_shaped(adequacy):
        msg = (
            "this manifest declares NOTHING — no seams, no edge cases, no test strategy, no acceptance "
            "criteria. That shape is the human-authorized plan-bypass and it waives the rubric gate, so it "
            "is recorded only through an audited authorization: a plan with nothing to declare is "
            "`ticket plan-bypass <id> --human-authorize <who> --reason <why>`."
        )
        raise ValueError(msg)
    if not is_valid_base_sha(base_sha):
        msg = (
            "a plan needs base_sha = the 40-char hex target-branch HEAD it was authored "
            f"against (got {base_sha[:12]!r}). Pass it so the plan can be bound to the base "
            "it planned against and detected as stale when the base moves. For work with no "
            "plan to record, use `ticket skip-planning` or `ticket plan-bypass`."
        )
        raise ValueError(msg)
    if not is_adequate(adequacy):
        msg = (
            "a plan needs a complete five-section adequacy manifest (design, integration_seams, "
            "edge_cases, test_strategy, acceptance_criteria); each section must be substantive OR "
            "carry an explicit reasoned negative (e.g. no_seams: <reason>). A scope-only thin spec "
            "has no seams/edge-cases/test-strategy claims and is refused — write a real plan, record "
            "explicit negatives, or use `ticket skip-planning` / `ticket plan-bypass`."
        )
        raise ValueError(msg)
