"""Append-only intake attachment manifest — the fetch-gate ledger (PR-15, M5).

Ticket intake fetches every spec attachment a ticket references *before* the
planner designs a plan: a PDF spec linked as a GitLab upload, a mockup on a
linked Notion page, a screenshot in a linked Slack thread. Before this model the
planner ran against the issue prose alone and silently missed the attached spec.

``AttachmentManifest`` makes the set of referenced attachments a *durable
artifact produced by the intake FSM step* (``execute_provision``, after the
worktrees materialise and before ``schedule_planning()``). It mirrors
:class:`LandscapeArtifact`: a dedicated append-only row alongside ``Ticket``, the
latest governs, older rows are an immutable audit trail. The gate
(:func:`teatree.core.attachment_manifest.attachment_gate_refusal`) reads the
latest manifest and refuses the planner hand-off while any entry is un-fetched.

Each entry is the JSON-serialisable shape ``{source_url, kind, local_path,
fetched_at}`` — ``kind`` is one of ``gitlab-upload`` / ``notion`` / ``slack``,
``local_path`` is where the file was cached under ``<ticket_dir>/.attachments/``
(empty until fetched), ``fetched_at`` an ISO timestamp (empty until fetched).
"""

from collections.abc import Sequence
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.db_retry import retry_on_locked


class AttachmentManifest(models.Model):
    """One immutable attachment-manifest snapshot persisted for a ticket (M5).

    An *empty* manifest is legitimate — a zero-attachment ticket surveyed and
    found nothing, which the gate reads as "clear". Only ``recorded_by`` is
    required so every snapshot is attributable; ``entries`` defaults to the empty
    list. ``latest_for`` returns the governing snapshot; the module-level
    :func:`teatree.core.attachment_manifest.build_manifest` owns the
    idempotent-append decision so a re-survey with an unchanged set writes no row.
    """

    ticket = models.ForeignKey(
        "core.Ticket",
        on_delete=models.CASCADE,
        related_name="attachment_manifests",
    )
    entries = models.JSONField(default=list)
    recorded_by = models.CharField(max_length=255, blank=True, default="")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_attachment_manifest"
        ordering: ClassVar = ["-recorded_at"]

    def __str__(self) -> str:
        return f"attachment-manifest<ticket:{self.ticket_id}:{len(self.entries or [])} entries>"  # type: ignore[attr-defined]  # Django FK accessor

    @classmethod
    def latest_for(cls, ticket: "models.Model") -> "AttachmentManifest | None":
        """The most recent manifest for *ticket* (``ordering`` puts it first), or None."""
        return cls.objects.filter(ticket=ticket).first()

    @classmethod
    def record(
        cls,
        *,
        ticket: "models.Model",
        entries: Sequence[dict[str, str]],
        recorded_by: str,
    ) -> "AttachmentManifest":
        """Guarded factory — the single path for persisting a manifest snapshot.

        *entries* is the JSON-serialisable list of ``{source_url, kind,
        local_path, fetched_at}`` dicts (possibly empty for a zero-attachment
        ticket). Validates it is a list and *recorded_by* is non-empty before
        writing any row — a blank author is refused so every snapshot is
        attributable. Construction is atomic so a rejected snapshot leaves no
        partial row.
        """
        if not isinstance(entries, Sequence) or isinstance(entries, str | bytes):
            msg = "entries must be a list of dicts"
            raise TypeError(msg)

        cleaned_author = recorded_by.strip() if recorded_by else ""
        if not cleaned_author:
            msg = "recorded_by is required and must be non-empty"
            raise ValueError(msg)

        stored = [dict(entry) for entry in entries]

        def _create() -> "AttachmentManifest":
            with transaction.atomic():
                return cls.objects.create(
                    ticket=ticket,
                    entries=stored,
                    recorded_by=cleaned_author,
                )

        return retry_on_locked(_create)
