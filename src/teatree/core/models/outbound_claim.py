"""Claim ledger for outbound posts made by the agent (#1019 outbound_audit).

Every time the agent (or one of the CLI surfaces it drives) publishes
something to a third-party system on the user's behalf — a Slack DM, a
GitLab note, a GitLab approval, a Notion comment/edit — we record one
``OutboundClaim`` row in the same transaction as the publish-success
path. The drift verifier scanner (``loop.scanners.outbound_audit``)
later picks up each claim and confirms the artifact actually exists in
the third-party system; if not, the claim is flipped to
``drift_detected=True`` and the user is DM'd by the overlay bot.

Why this matters: "I said I posted X" is one of the easiest agent
failure modes to hide. Transport returns 200, log says "OK posted", but
the actual API state never received the call (race, retry collapse,
phantom success). A claim ledger + verifier is the cheapest "did the
thing I claimed actually happen?" gate.

Out of scope of ``BotPing`` (#963): ``BotPing`` audits *notify_user*'s
own happy/sad path so the helper itself is idempotent. ``OutboundClaim``
covers every outbound surface uniformly so the drift verifier has one
table to scan, not N.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class OutboundClaim(models.Model):
    """One outbound artifact the agent claims to have published.

    The verifier sets ``verified_at`` once a third-party GET confirms the
    artifact exists, or sets ``drift_detected=True`` + ``drift_reason``
    + ``drift_alerted_at`` once it has DM'd the user about the drift.
    Dedupe on ``drift_alerted_at`` keeps the same drift from re-firing
    every tick — see :class:`OutboundAuditScanner`.
    """

    class Kind(models.TextChoices):
        SLACK_DM = "slack_dm", "Slack DM"
        SLACK_REACTION = "slack_reaction", "Slack reaction"
        GITLAB_NOTE = "gitlab_note", "GitLab note"
        GITLAB_APPROVE = "gitlab_approve", "GitLab approve"
        GITHUB_NOTE = "github_note", "GitHub note"
        NOTION_COMMENT = "notion_comment", "Notion comment"
        NOTION_EDIT = "notion_edit", "Notion edit"

    agent_session_id = models.CharField(max_length=255, blank=True)
    kind = models.CharField(max_length=32, choices=Kind.choices)
    target_url = models.URLField(max_length=1024, blank=True)
    idempotency_key = models.CharField(max_length=255, unique=True)
    claim_ts = models.DateTimeField(default=timezone.now)
    verified_at = models.DateTimeField(null=True, blank=True)
    drift_detected = models.BooleanField(default=False)
    drift_reason = models.TextField(blank=True)
    drift_alerted_at = models.DateTimeField(null=True, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "teatree_outbound_claim"
        ordering: ClassVar = ["-claim_ts"]
        indexes: ClassVar = [
            models.Index(fields=["kind", "claim_ts"]),
            models.Index(fields=["verified_at", "drift_detected"]),
            models.Index(fields=["drift_detected", "drift_alerted_at"]),
        ]

    def __str__(self) -> str:
        status = "verified" if self.verified_at is not None else "drift" if self.drift_detected else "pending"
        return f"OutboundClaim[{self.kind}/{status}] {self.idempotency_key}"
