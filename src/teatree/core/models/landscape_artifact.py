"""Append-only intake landscape artifact — the survey the planner consumes (#2541).

The ticket-intake (info-fetch) FSM step surveys what is **already in flight or
already settled** before the planner designs a plan — open PRs/MRs, local
worktrees carrying uncommitted or unpushed work, and a per-issue
close/merge/supersede recommendation (gathered by :mod:`teatree.core.landscape`).
Before #2541 that survey was an on-demand CLI an operator ran and pasted into the
plan prose; the planner re-derived it when it was not handed over.

``LandscapeArtifact`` makes the survey a *durable artifact produced by the intake
FSM step* (the ``execute_provision`` worker, after worktrees materialise and
before ``schedule_planning()``). The planner then consumes the persisted survey
instead of re-deriving it. The model mirrors :class:`PlanArtifact`: a dedicated
append-only row alongside ``Ticket``, never a session-volatile JSON file. The
latest artifact governs; older rows are an immutable audit trail.

The payload is the JSON-serialisable survey shape (open PRs, in-flight worktrees,
per-issue recommendations, warnings) — the same dict the ``workspace landscape``
command renders — stored verbatim so the planner reads structured data, not prose.
"""

from collections.abc import Mapping
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.db_retry import retry_on_locked


class LandscapeArtifact(models.Model):
    """One immutable intake-landscape survey persisted for a ticket (#2541).

    Written by the intake FSM worker (``execute_provision``) once the worktree
    layout exists, and consumed by the planner. The guarded factory
    (:meth:`record`) refuses an empty survey so a vacuous artifact is never
    persisted. ``warnings`` count is denormalised onto ``survey`` itself — the
    payload is honest about probes it could not complete.
    """

    ticket = models.ForeignKey(
        "core.Ticket",
        on_delete=models.CASCADE,
        related_name="landscape_artifacts",
    )
    survey = models.JSONField(default=dict)
    recorded_by = models.CharField(max_length=255, blank=True, default="")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_landscape_artifact"
        ordering: ClassVar = ["-recorded_at"]

    def __str__(self) -> str:
        return f"landscape-artifact<ticket:{self.ticket_id}@{self.recorded_at.isoformat()[:19]}>"  # type: ignore[attr-defined]  # Django FK accessor

    @classmethod
    def latest_for(cls, ticket: "models.Model") -> "LandscapeArtifact | None":
        """The most recent survey for *ticket* (``ordering`` puts it first), or None."""
        return cls.objects.filter(ticket=ticket).first()

    @classmethod
    def record(
        cls,
        *,
        ticket: "models.Model",
        survey: Mapping[str, object],
        recorded_by: str,
    ) -> "LandscapeArtifact":
        """Guarded factory — the single path for persisting a landscape survey.

        *survey* is the JSON-serialisable landscape report (a plain ``dict`` from
        the manual CLI/test path, or the ``LandscapeReport`` TypedDict from the
        intake worker — both are ``Mapping[str, object]``). Validates that it is a
        non-empty mapping and *recorded_by* is non-empty before writing any row:
        an empty survey carries no intake signal, so it is refused with
        ``ValueError`` rather than persisted as a vacuous artifact the planner
        would consume as "nothing in flight". A blank author is refused
        symmetrically so every artifact is attributable. Construction is atomic so
        a rejected artifact leaves no partial row.
        """
        if not isinstance(survey, Mapping) or not survey:
            msg = "survey is required and must be a non-empty dict"
            raise ValueError(msg)

        cleaned_author = recorded_by.strip() if recorded_by else ""
        if not cleaned_author:
            msg = "recorded_by is required and must be non-empty"
            raise ValueError(msg)

        stored = dict(survey)

        def _create() -> "LandscapeArtifact":
            with transaction.atomic():
                return cls.objects.create(
                    ticket=ticket,
                    survey=stored,
                    recorded_by=cleaned_author,
                )

        return retry_on_locked(_create)
