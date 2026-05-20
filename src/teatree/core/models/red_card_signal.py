"""Durable ledger row for a user RED CARD signal (#1130).

A RED CARD is the user's signal that the agent did something structurally
wrong and must fix it *upstream in teatree*, not just behaviourally. The
user surfaces three forms:

* ``:red_circle:`` reaction on the agent's prior message,
* ``:no_entry_sign:`` reaction on the agent's prior message,
* the literal phrase ``"RED CARD"`` (optionally ``"red-card"`` /
    ``"red card"``, case-insensitive) in a DM or thread reply.

Each fresh signal lands one row of this model, keyed idempotently on
``(overlay, channel, slack_ts)`` so the scanner can over-poll without
double-recording. The lifecycle is monotonic ``pending`` →
``eyes_added`` (after the bot acks with ``:eyes:``) → ``issue_filed``
(after the coordinator records the upstream teatree issue URL that
captures the gap) → ``resolved`` (after the upstream fix lands).

Mirrors :class:`teatree.core.models.review_assignment.ReviewAssignment`
in shape: a typed :class:`RedCardIntent` dataclass for the origin
payload, an idempotent :meth:`record` classmethod, and short
state-advancing methods. The signal itself does not file the upstream
issue — that work is the coordinator's, dispatched from the
``red_card.signal`` scanner emission. The model only persists the
ledger so the coordinator's eventual issue URL has somewhere to land.
"""

from dataclasses import dataclass
from typing import ClassVar

from django.db import models
from django.utils import timezone


@dataclass(frozen=True, slots=True)
class RedCardIntent:
    """Origin metadata for a :class:`RedCardSignal` row.

    The ``(overlay, channel, slack_ts)`` triple is the idempotency key.
    ``signal_kind`` records which surface produced the signal so the
    audit trail can answer "was this a reaction or a text signal?" and
    "which emoji?". ``offending_message_ts`` + ``offending_message_text``
    point at the agent message the user red-carded (empty for text
    signals where the user is calling out a general behaviour rather
    than a specific message).
    """

    overlay: str
    channel: str
    slack_ts: str
    signal_kind: str
    user_id: str
    offending_message_ts: str = ""
    offending_message_text: str = ""
    signal_text: str = ""


class RedCardSignal(models.Model):
    """One user RED CARD signal — a corrective-action trigger (#1130)."""

    class Kind(models.TextChoices):
        RED_CIRCLE = "red_circle", "Red Circle reaction"
        NO_ENTRY_SIGN = "no_entry_sign", "No Entry Sign reaction"
        RED_CARD_TEXT = "red_card_text", "Literal 'RED CARD' text"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        EYES_ADDED = "eyes_added", "Eyes Added"
        ISSUE_FILED = "issue_filed", "Issue Filed"
        RESOLVED = "resolved", "Resolved"

    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    signal_kind = models.CharField(max_length=32, choices=Kind.choices)
    user_id = models.CharField(max_length=64)
    offending_message_ts = models.CharField(max_length=64, blank=True, default="")
    offending_message_text = models.TextField(blank=True, default="")
    signal_text = models.TextField(blank=True, default="")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    observed_at = models.DateTimeField(default=timezone.now)
    eyes_reacted_at = models.DateTimeField(null=True, blank=True)
    filed_issue_url = models.URLField(max_length=500, blank=True, default="")
    issue_filed_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_red_card_signal"
        ordering: ClassVar = ["observed_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["overlay", "channel", "slack_ts"],
                name="uniq_redcardsignal_overlay_channel_ts",
            ),
        ]

    def __str__(self) -> str:
        return f"red-card<{self.pk}:{self.state} {self.signal_kind} user={self.user_id}>"

    @classmethod
    def record(cls, intent: RedCardIntent) -> "RedCardSignal | None":
        """Insert one row idempotently on ``(overlay, channel, slack_ts)``.

        Returns the new row on first observation, ``None`` when the row
        already exists. The scanner treats ``None`` as "we already saw
        this — skip" so the same Slack event never produces two
        coordinator dispatches.
        """
        if not intent.slack_ts or not intent.channel:
            return None
        row, created = cls.objects.get_or_create(
            overlay=intent.overlay,
            channel=intent.channel,
            slack_ts=intent.slack_ts,
            defaults={
                "signal_kind": intent.signal_kind,
                "user_id": intent.user_id,
                "offending_message_ts": intent.offending_message_ts,
                "offending_message_text": intent.offending_message_text,
                "signal_text": intent.signal_text,
            },
        )
        return row if created else None

    def mark_eyes_added(self) -> bool:
        """Mark this row as ``eyes_added`` after the bot posted ``:eyes:``.

        Idempotent: a second call is a no-op. Returns ``True`` only on
        the actual transition so the caller can emit audit lines once.
        """
        now = timezone.now()
        updated = (
            type(self)
            .objects.filter(pk=self.pk, state=self.State.PENDING)
            .update(state=self.State.EYES_ADDED, eyes_reacted_at=now)
        )
        if updated:
            self.refresh_from_db(fields=["state", "eyes_reacted_at"])
        return bool(updated)

    def link_issue(self, url: str) -> bool:
        """Stamp the upstream teatree issue URL and advance to ``issue_filed``.

        Called from the coordinator workflow after it identifies the
        upstream gap and files the corrective teatree issue. Empty URLs
        are rejected — the gate must not be satisfied without a real
        issue link.
        """
        if not url:
            return False
        now = timezone.now()
        updated = (
            type(self)
            .objects.filter(pk=self.pk)
            .exclude(state=self.State.RESOLVED)
            .update(
                state=self.State.ISSUE_FILED,
                filed_issue_url=url,
                issue_filed_at=now,
            )
        )
        if updated:
            self.refresh_from_db(fields=["state", "filed_issue_url", "issue_filed_at"])
        return bool(updated)
