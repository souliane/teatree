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
    drain reads unconsumed rows for the t3-master session, emits them
    into ``additionalContext``, and stamps ``consumed_at`` so a re-fire
    of the hook is a clean no-op. ``answered_at`` is the orthogonal gate:
    set when the agent actually replies to the user (via
    :meth:`agent_answered_question` or the ``notify_user`` integration in
    :mod:`teatree.core.notify`).
    """

    class AnswerKind(models.TextChoices):
        UNANSWERED = "", "Unanswered"
        ACK = "ack", "Ack"
        SIMPLE = "simple", "Simple"
        DELEGATED = "delegated", "Delegated"
        QUESTION_REPLY = "question_reply", "Question reply"
        # The reactive answerer skips a DM the user authored themselves — an
        # instruction or an on-behalf outbound echo, never an inbound question
        # for the loop to answer (#1941). Stamped so the row leaves the queue.
        SELF = "self", "Self-authored (skipped)"

    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    user_id = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField()
    received_at = models.DateTimeField(default=timezone.now)
    consumed_at = models.DateTimeField(null=True, blank=True)
    # #1069's strict turn-end gate column: set ONLY when the agent
    # personally replied to the user (via ``agent_answered_question`` or
    # the ``notify_user`` integration in :mod:`teatree.core.notify`).
    # ``db_index=True`` because ``unanswered_questions_since`` filters on
    # it on the Stop-hook hot path. The reactive Slack-answer loop must
    # NOT write this column — see ``loop_replied_at`` below (#1075).
    answered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # The reactive Slack-answer loop (#1014) stamps these; they are
    # orthogonal to BOTH ``consumed_at`` (the prompt-drain column) and
    # ``answered_at`` (#1069's agent-personally-replied gate). Option B
    # (#1075): the loop owns ``loop_replied_at``, a column distinct from
    # ``answered_at``, so the loop posting a token-cheap reply does NOT
    # satisfy the #1063 turn-end gate — that gate stays a strict "the
    # agent personally answered" guarantee, fully decoupled from the loop
    # work-queue. A row may be consumed-but-loop-unreplied (drained into a
    # prompt, no loop reply yet) or loop-replied-but-unconsumed (the loop
    # replied before any interactive session drained it). Each is a
    # single-use compare-and-swap, never written for the same column twice.
    loop_replied_at = models.DateTimeField(null=True, blank=True)
    answer_kind = models.CharField(
        max_length=16,
        blank=True,
        default="",
        choices=AnswerKind.choices,
    )
    eyes_reacted_at = models.DateTimeField(null=True, blank=True)

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
    def is_loop_replied(self) -> bool:
        """True once the reactive Slack-answer loop has replied (#1075).

        Distinct from ``answered_at`` (#1069's "the agent personally
        replied" gate): the loop stamps ``loop_replied_at``, never
        ``answered_at``, so this property never reflects the turn-end
        gate's state.
        """
        return self.loop_replied_at is not None

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

    @classmethod
    def loop_unreplied(cls, *, overlay: str = "") -> models.QuerySet["PendingChatInjection"]:
        """Return the reactive Slack-answer loop's work-queue, oldest first.

        Orthogonal to BOTH :meth:`pending` (the ``consumed_at`` prompt-
        drain queue) and the #1069 turn-end gate: this gates on
        ``loop_replied_at`` (the loop's own column, #1075 / Option B),
        NOT ``answered_at``. Decoupling the loop work-queue from
        ``answered_at`` is the whole point — the loop posting a reply
        stamps only ``loop_replied_at``, so it never silently satisfies
        the #1063 Stop-hook turn-end gate (which still requires the agent
        to *personally* answer via ``agent_answered_question``). A row
        drained into a prompt is still loop-unreplied until the loop posts,
        so the answer loop and the prompt-drain never double-process the
        same column.

        Pass ``overlay=""`` to scan every overlay's queue (the v1 single-
        overlay path uses ``overlay=""`` consistently).
        """
        qs = cls.objects.filter(loop_replied_at__isnull=True)
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

    def mark_loop_replied(self, kind: str) -> bool:
        """Stamp ``loop_replied_at`` + ``answer_kind``; ``True`` on the transition.

        Single-use compare-and-swap (``UPDATE … WHERE loop_replied_at IS
        NULL``) mirroring :meth:`consume`: a concurrent second caller sees
        0 rows updated and returns ``False`` without overwriting the first
        ``answer_kind``. Writes ONLY the reactive Slack-answer loop's
        column (#1075 / Option B) — never ``consumed_at`` (the prompt-
        drain column) and never ``answered_at`` (#1069's strict "the
        agent personally replied" turn-end gate). The loop replying must
        not satisfy that gate.
        """
        updated = (
            type(self)
            .objects.filter(pk=self.pk, loop_replied_at__isnull=True)
            .update(loop_replied_at=timezone.now(), answer_kind=kind)
        )
        if updated:
            self.refresh_from_db(fields=["loop_replied_at", "answer_kind"])
        return bool(updated)

    def mark_self_skipped(self) -> bool:
        """Retire a row the user authored themselves from the answerer queue (#1941).

        Stamps ``loop_replied_at`` with the ``SELF`` kind so the row exits
        ``loop_unreplied()`` and never yields an answering task, while leaving
        ``consumed_at`` / ``answered_at`` untouched — the prompt-drain still
        surfaces the user's own DM into context; only the reactive auto-answerer
        skips it. Single-use CAS via :meth:`mark_loop_replied`.
        """
        return self.mark_loop_replied(self.AnswerKind.SELF)

    def unmark_loop_replied(self) -> bool:
        """Release the loop-reply claim; ``True`` if a stamp was cleared, else ``False``.

        The rollback half of :meth:`mark_loop_replied`: when the side-effect
        of a claimed loop reply (the ACK :white_check_mark: reaction) fails,
        the caller clears ``loop_replied_at`` + ``answer_kind`` so the unit
        re-enters ``loop_unreplied()`` and is retried next cycle instead of
        carrying a receipt for a reply that never landed. The conditional
        ``UPDATE … WHERE loop_replied_at IS NOT NULL`` only ever clears a
        present claim.
        """
        updated = (
            type(self)
            .objects.filter(pk=self.pk, loop_replied_at__isnull=False)
            .update(loop_replied_at=None, answer_kind="")
        )
        if updated:
            self.refresh_from_db(fields=["loop_replied_at", "answer_kind"])
        return bool(updated)

    def mark_eyes_reacted(self) -> bool:
        """Stamp ``eyes_reacted_at``; ``True`` on the transition, else ``False``.

        Single-use CAS so the no-LLM :eyes: receipt-acknowledgement
        reaction fires at most once even when the answer cycle re-runs the
        same row across ticks (post/readback failures leave the row
        loop-unreplied for retry, but the :eyes: must not re-post). The CAS
        is the *claim*: the cycle stamps it BEFORE reacting so a concurrent
        cycle cannot also react, then releases it with
        :meth:`unmark_eyes_reacted` if the reaction fails, so the next cycle
        retries (claim -> side-effect -> release-on-failure).
        """
        updated = (
            type(self).objects.filter(pk=self.pk, eyes_reacted_at__isnull=True).update(eyes_reacted_at=timezone.now())
        )
        if updated:
            self.refresh_from_db(fields=["eyes_reacted_at"])
        return bool(updated)

    def unmark_eyes_reacted(self) -> bool:
        """Release the :eyes: claim; ``True`` if a stamp was cleared, else ``False``.

        The rollback half of :meth:`mark_eyes_reacted`: when the :eyes:
        reaction fails after the claim, the cycle clears ``eyes_reacted_at``
        so the row is reacted again next cycle instead of carrying a receipt
        for a reaction that never landed. The conditional ``UPDATE … WHERE
        eyes_reacted_at IS NOT NULL`` only ever clears a present stamp.
        """
        updated = type(self).objects.filter(pk=self.pk, eyes_reacted_at__isnull=False).update(eyes_reacted_at=None)
        if updated:
            self.refresh_from_db(fields=["eyes_reacted_at"])
        return bool(updated)

    @classmethod
    def retire_answered_in_thread(cls, thread_ts: str) -> int:
        """Retire the row a bot→user threaded DM reply answers (#2053).

        When the agent answers a queued user question out-of-band — a
        threaded reply through the ``notify post --thread-ts`` egress, not
        the reactive Slack-answer cycle — the row it answers is the one
        whose ``slack_ts`` is this reply's ``thread_ts`` (a Slack thread
        roots on the question's ts). That row gets stamped on BOTH gates in
        one transition: ``loop_replied_at`` so the cycle's
        :meth:`loop_unreplied` work-queue retires it (and never re-delegates
        a ``t3:answerer`` Task), and ``answered_at`` so the #1063 Stop-hook
        gate stops nagging. The threaded reply IS the agent personally
        answering, so it satisfies both — unlike the cycle's own
        token-cheap reply, which deliberately stamps only ``loop_replied_at``.

        Single-use compare-and-swap on ``loop_replied_at IS NULL`` mirroring
        :meth:`mark_loop_replied`: a row already loop-replied (by the cycle
        or a prior reply) is left untouched, so a stable ``answer_kind`` is
        never overwritten. Returns the number of rows transitioned. The
        empty ``thread_ts`` (a top-level DM, not a reply) matches nothing
        and returns ``0``.
        """
        if not thread_ts:
            return 0
        return int(
            cls.objects.filter(slack_ts=thread_ts, loop_replied_at__isnull=True).update(
                loop_replied_at=timezone.now(),
                answered_at=timezone.now(),
                answer_kind=cls.AnswerKind.QUESTION_REPLY,
            )
        )

    @classmethod
    def agent_answered_question(cls, slack_ts: str) -> int:
        """Stamp ``answered_at = now`` on rows matching ``slack_ts``.

        Returns the number of rows actually transitioned from
        ``answered_at IS NULL`` to ``answered_at = now``. Idempotent: a
        second call on an already-answered row is a no-op and returns
        ``0``. The empty ``slack_ts`` is rejected — there is no row that
        the empty string could legitimately identify.

        Gate/satisfier symmetry: the stamp is keyed on ``slack_ts`` alone,
        exactly mirroring the unscoped ``unanswered_questions_since`` gate.
        ``slack_ts`` is the unique idempotency key — a Slack message has
        exactly one ``ts`` per channel and the user has a single DM — so a
        single stamp keyed on it clears precisely the row the gate sees and
        cannot cross-stamp another. This is what makes a concurrent multi-
        overlay deployment work: a session under one overlay answers a
        question recorded under a *different* overlay (the recording overlay
        and the answering session's ``T3_OVERLAY_NAME`` routinely differ).
        Scoping the stamp by overlay added no correctness — only the broken
        narrowing that stranded the row unanswered and nagged forever.
        """
        if not slack_ts:
            return 0
        return int(cls.objects.filter(slack_ts=slack_ts, answered_at__isnull=True).update(answered_at=timezone.now()))

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
