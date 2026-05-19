"""Durable Slack-DM-inbound queue (#1014, BLUEPRINT §17.1 invariant 2 / §5.6).

The Slack inbound bridge: a user message DM'd to the overlay bot lands in
this queue as a single :class:`PendingChatInjection` row. The next
``UserPromptSubmit`` handler reads unconsumed rows, formats them as an
``additionalContext`` block, and marks them ``consumed_at``, so the agent
sees them as if the user typed them in Claude Code chat.

Mirrors the :class:`teatree.core.models.deferred_question.DeferredQuestion`
shape — durable, single-use, scoped, idempotent — applied to the *reverse*
direction (user → agent). The Slack ``ts`` is the canonical idempotency
key: the scanner can over-poll safely because ``unique(overlay, ts)``
deduplicates, and the injection handler is safe to re-fire because
``consumed_at`` is stamped once.

Issue #1063 adds the ``answered_at`` gate. ``consumed_at`` only proves the
agent *read* the row into context; it does not prove the agent *replied*
to the user's question. Empirically (2026-05-19), drain worked perfectly
while the agent silently ignored ~22 of 25 user questions in a single
day. ``answered_at`` is the structural answer: a Stop hook soft-blocks
the turn while any heuristic-classified question from the last hour has
``answered_at IS NULL``. The heuristic lives here as :attr:`is_question`
so the model is the single source of truth for "this row needs a reply".
"""

import re
from datetime import timedelta
from typing import ClassVar

from django.db import models
from django.utils import timezone

_QUESTION_WORDS: frozenset[str] = frozenset(
    {
        "why",
        "what",
        "when",
        "where",
        "who",
        "which",
        "how",
        "is",
        "are",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
        "was",
        "were",
    }
)

_QUESTION_PHRASES: tuple[str, ...] = (
    "please answer",
    "please explain",
    "please tell me",
)

# Strip leading whitespace, punctuation, and markdown decoration
# (``*``, ``_``, ``>``, ``-``, backticks, list-bullet digits + ``.``/``)``)
# before applying the heuristic. ``re.UNICODE`` is implicit in py3.
_LEADING_NOISE = re.compile(r"^[\s*_\->`#0-9.()]+")

_FIRST_WORD = re.compile(r"^([A-Za-z]+)")


class PendingChatInjection(models.Model):
    """One Slack DM from the user waiting to be injected into the next prompt.

    The scanner inserts a row per new message; the ``UserPromptSubmit``
    drain reads unconsumed rows for the loop-owner session, emits them
    into ``additionalContext``, and stamps ``consumed_at`` so a re-fire
    of the hook is a clean no-op. ``answered_at`` is the orthogonal gate:
    set when the agent actually replies to the user (via
    :meth:`agent_answered_question` or the ``notify_user`` integration in
    :mod:`teatree.core.notify`).
    """

    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    user_id = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField()
    received_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        db_table = "teatree_pending_chat_injection"
        ordering: ClassVar = ["received_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["overlay", "slack_ts"], name="uniq_pendingchat_overlay_ts"),
        ]

    def __str__(self) -> str:
        status = "consumed" if self.consumed_at else "pending"
        if self.answered_at is not None:
            status = "answered"
        return f"pending-chat-injection<{self.pk}:{status} overlay={self.overlay!r} ts={self.slack_ts}>"

    @property
    def is_pending(self) -> bool:
        return self.consumed_at is None

    @property
    def is_question(self) -> bool:
        """Heuristic: does ``text`` look like a user question requiring a reply?

        True when, after stripping leading whitespace / punctuation /
        markdown decoration, ANY of the following holds: the stripped
        text ends with ``?``; the first word (case-insensitive) is in
        :data:`_QUESTION_WORDS`; or the stripped text contains one of
        ``please answer`` / ``please explain`` / ``please tell me``.

        Tuned against the 25 real user-question texts in production
        (#1063). The empty string returns ``False``.
        """
        return _classify_is_question(self.text)

    @classmethod
    def record(
        cls,
        *,
        channel: str,
        slack_ts: str,
        text: str,
        overlay: str = "",
        user_id: str = "",
    ) -> "PendingChatInjection | None":
        """Insert one row idempotently on ``(overlay, slack_ts)``.

        Returns the new row, or ``None`` if a row for this ``(overlay, ts)``
        already exists (the scanner over-polled). The ``ts`` is the
        canonical idempotency key — Slack guarantees uniqueness per
        channel and the scanner only ever sees one channel per overlay.
        """
        if not slack_ts or not channel or not text.strip():
            return None
        row, created = cls.objects.get_or_create(
            overlay=overlay,
            slack_ts=slack_ts,
            defaults={
                "channel": channel,
                "user_id": user_id,
                "text": text,
            },
        )
        return row if created else None

    @classmethod
    def pending(cls, *, overlay: str = "") -> models.QuerySet["PendingChatInjection"]:
        """Return the unconsumed queue for *overlay*, oldest first.

        Pass ``overlay=""`` to drain every overlay's queue (the v1 single-
        overlay path uses ``overlay=""`` consistently and ignores filter).
        """
        qs = cls.objects.filter(consumed_at__isnull=True)
        if overlay:
            qs = qs.filter(overlay=overlay)
        return qs.order_by("received_at")

    def consume(self) -> bool:
        """Mark this row consumed; return ``True`` on the transition, else ``False``.

        Idempotent: a second call on an already-consumed row is a no-op
        and returns ``False``. Returning the transition lets the caller
        emit audit lines only once.
        """
        updated = type(self).objects.filter(pk=self.pk, consumed_at__isnull=True).update(consumed_at=timezone.now())
        if updated:
            self.refresh_from_db(fields=["consumed_at"])
        return bool(updated)

    @classmethod
    def agent_answered_question(cls, slack_ts: str, *, overlay: str = "") -> int:
        """Stamp ``answered_at = now`` on rows matching ``(overlay, slack_ts)``.

        Returns the number of rows actually transitioned from
        ``answered_at IS NULL`` to ``answered_at = now``. Idempotent: a
        second call on an already-answered row is a no-op and returns
        ``0``. The empty ``slack_ts`` is rejected — there is no row that
        the empty string could legitimately identify.

        The ``(overlay, slack_ts)`` pair is the natural idempotency key
        (the same one ``UniqueConstraint`` enforces on ingest), so the
        agent reply doesn't need the row's primary key — just the Slack
        ts of the question it is answering, which is already in the
        ``additionalContext`` payload the agent saw.
        """
        if not slack_ts:
            return 0
        return int(
            cls.objects.filter(
                overlay=overlay,
                slack_ts=slack_ts,
                answered_at__isnull=True,
            ).update(answered_at=timezone.now())
        )

    @classmethod
    def unanswered_questions_since(cls, window: timedelta) -> list["PendingChatInjection"]:
        """Return question rows received within *window* that are unanswered.

        Used by the Stop hook (#1063): the hook fires at every turn end
        and queries this method to decide whether to emit a blocking
        reminder. The heuristic filter runs in Python because
        :attr:`is_question` is a property, not a stored column — the row
        count for a single hour is small (45 in the worst observed day)
        so the in-Python filter is fine.

        Rows where ``answered_at`` is already set are skipped; rows
        outside the window are skipped (older questions are stale —
        nudging on them produces noise without changing behaviour).
        """
        cutoff = timezone.now() - window
        rows = cls.objects.filter(
            received_at__gte=cutoff,
            answered_at__isnull=True,
        ).order_by("received_at")
        return [row for row in rows if row.is_question]


def _classify_is_question(text: str) -> bool:
    """Pure-function heuristic backing :attr:`PendingChatInjection.is_question`.

    Split out so the heuristic can be tested directly without round-
    tripping through the ORM. See :attr:`PendingChatInjection.is_question`
    for the spec.
    """
    if not text:
        return False
    stripped = _LEADING_NOISE.sub("", text).strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    lowered = stripped.lower()
    for phrase in _QUESTION_PHRASES:
        if phrase in lowered:
            return True
    match = _FIRST_WORD.match(stripped)
    if match is None:
        return False
    return match.group(1).lower() in _QUESTION_WORDS


__all__ = ["PendingChatInjection"]
