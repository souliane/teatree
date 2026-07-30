"""Durable session-to-session work handover.

A :class:`SessionHandover` row carries one session's full durable-state
snapshot (the same payload the PreCompact hook already builds: active
tickets, worktree paths/branches, in-flight sub-agents, open PRs,
approach/decisions, failing tests, loaded skills, t3-master status) to
another session — either a named ``to_session`` or, when null, "the next
session to start". The takeover is zero-copy-paste: the SessionStart hook
claims an unclaimed handover targeted at the starting session (or at "next
session") and injects the payload as ``additionalContext``.

Mirrors the :class:`teatree.core.models.pending_chat_injection.PendingChatInjection`
shape — durable, single-use, scoped, idempotent. The DB row is the source
of truth; the XDG file mirror (``handover_mirror_path``) is for
human-readability and for bootstrapping a brand-new session whose process
predates any DB it can read.

Claiming is a backend-agnostic compare-and-swap (a conditional ``UPDATE``
gated on ``claimed_at IS NULL``), NOT ``select_for_update`` — teatree's
production DB is SQLite where ``skip_locked`` is silently dropped (#786 B1).
Exactly one of N racing SessionStart hooks wins the claim; the losers see
0 rows updated and inject nothing.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.managers import SessionHandoverManager


class SessionHandover(models.Model):
    """One pending hand-off of a session's durable state to another session."""

    from_session = models.CharField(max_length=255)
    to_session = models.CharField(max_length=255, blank=True, default="")
    payload = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_by = models.CharField(max_length=255, blank=True, default="")

    objects = SessionHandoverManager()

    class Meta:
        db_table = "teatree_session_handover"
        indexes: ClassVar = [models.Index(fields=["to_session", "claimed_at"])]

    def __str__(self) -> str:
        target = self.to_session or "next-session"
        return f"handover<{self.from_session} -> {target}>"

    @property
    def is_for_next_session(self) -> bool:
        """True iff this handover targets whichever session starts next (no explicit target)."""
        return not self.to_session
