"""Per-send provenance/delegation audit for the outbound send-proxy (#117, M-C).

Every outbound artifact that routes through :mod:`teatree.core.send_proxy`
writes one ``SendAudit`` row: the destination it went to, the allowlist verdict
the proxy computed, whether redaction fired, and — the part #119 consumes — the
delegation provenance (which directive / ticket / human authorized the send).

Sibling of :class:`~teatree.core.models.on_behalf_approval.OnBehalfAudit`
(who approved a colleague post) and
:class:`~teatree.core.models.outbound_claim.OutboundClaim` (did the post I
claimed actually land): ``OnBehalfAudit`` audits the *approval*, ``OutboundClaim``
audits *delivery drift*, and ``SendAudit`` audits the *policy decision* at the one
outbound chokepoint. It is the ledger an operator reads to seed the per-overlay
destination allowlist from a live-traffic soak before flipping the proxy from
``warn`` to ``enforce`` (the ship-safe rollout).

The row is best-effort — a failed audit write never blocks or rolls back the send
(the proxy swallows the DB error) — so it can lag the wire call but never breaks it.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class SendAudit(models.Model):
    """One outbound send the proxy evaluated — its destination, verdict, and delegation.

    ``allowlist_verdict`` records what the allowlist check produced regardless of
    ``mode``: in ``warn`` mode a non-allowlisted destination is stamped
    :attr:`Verdict.WARNED` (audited, not blocked); in ``enforce`` mode the same
    destination is stamped :attr:`Verdict.DENIED` (blocked). An allowlisted (or
    self-DM) destination is :attr:`Verdict.ALLOWED` under both modes.
    """

    class Channel(models.TextChoices):
        SLACK = "slack", "Slack"
        GITHUB = "github", "GitHub"
        GITLAB = "gitlab", "GitLab"
        OTHER = "other", "Other"

    class Verdict(models.TextChoices):
        ALLOWED = "allowed", "Allowed"
        WARNED = "warned", "Warned (audit-only, not blocked)"
        DENIED = "denied", "Denied (enforce-mode block)"

    channel = models.CharField(max_length=16, choices=Channel.choices)
    destination = models.CharField(max_length=512, blank=True)
    action = models.CharField(max_length=64, blank=True)
    target = models.CharField(max_length=512, blank=True)
    overlay = models.CharField(max_length=255, blank=True)
    mode = models.CharField(max_length=16)
    allowlist_verdict = models.CharField(max_length=16, choices=Verdict.choices)
    redaction_applied = models.BooleanField(default=False)
    redaction_matches = models.JSONField(default=list, blank=True)
    #: Delegation provenance the #119 per-action-class dial reads: the trust
    #: origin of the content (a ``Provenance`` value) plus the free-form
    #: ``authorized_by`` ref (``directive:<id>`` / ``ticket:<id>`` / a human id)
    #: naming which authority sanctioned this send.
    provenance = models.CharField(max_length=32, blank=True)
    authorized_by = models.CharField(max_length=255, blank=True)
    agent_session_id = models.CharField(max_length=255, blank=True)
    payload_summary = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_send_audit"
        ordering: ClassVar = ["-created_at"]
        indexes: ClassVar = [
            models.Index(fields=["channel", "destination"]),
            models.Index(fields=["overlay", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"send-audit<{self.channel}:{self.destination} {self.allowlist_verdict} ({self.mode})>"
