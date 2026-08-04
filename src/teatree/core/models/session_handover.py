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
shape — durable, single-use, scoped, idempotent. The DB row is the DELIVERY
SURFACE, not merely the record of one: the SessionStart pickup reads the XDG
file mirror (``handover_mirror_path``) ONLY when the DB is unreachable. A
readable DB that returns nothing delivers nothing — reading the mirror there is
how four unclaimed rows were passed over while one stale file was injected in
their place (#4194).

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
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID


class SessionHandover(models.Model):
    """One pending hand-off of a session's durable state to another session.

    An author holds at most ONE unclaimed row and later hand-offs are absorbed
    into it, so ``created_at`` means "when the current payload was written", not
    "when this hand-off first existed". That is the key both the parked tier's
    oldest-first drain order and :func:`~teatree.core.handover.unique_mirror_path`
    want: a row whose payload just changed is the newest state, and its mirror
    file is named after the write that produced it.
    """

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
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["from_session"],
                condition=models.Q(claimed_at__isnull=True),
                name="uniq_unclaimed_handover_per_from_session",
            ),
            # A PENDING row claimable by NOBODY is unrepresentable, not merely refused
            # at the manager: ``claimable_for`` admits only the session named by
            # ``to_session`` and excludes the one named by ``from_session``. Two
            # disjuncts are load-bearing — an anonymous parked row IS claimable by
            # anyone, and a CLAIMED row was already delivered, so its target is a
            # historical record rather than an address anyone still has to resolve.
            models.CheckConstraint(
                condition=(
                    models.Q(claimed_at__isnull=False)
                    | models.Q(to_session="")
                    | ~models.Q(to_session=models.F("from_session"))
                ),
                name="ck_handover_target_not_self",
            ),
            models.CheckConstraint(
                condition=models.Q(claimed_at__isnull=False) | ~models.Q(to_session=LOOP_RUNNER_SESSION_ID),
                name="ck_handover_target_not_loop_runner",
            ),
        ]

    def __str__(self) -> str:
        target = self.to_session or "next-session"
        return f"handover<{self.from_session} -> {target}>"

    @property
    def is_for_next_session(self) -> bool:
        """True iff this handover targets whichever session starts next (no explicit target)."""
        return not self.to_session
