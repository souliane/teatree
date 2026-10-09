"""Durable Slack-DM-inbound queue (#1014, BLUEPRINT §17.1 invariant 2 / §5.6).

The Slack inbound bridge: a user message DM'd to the overlay bot lands in
this queue as a single :class:`PendingChatInjection` row, which the reactive
Slack-answer cycle consumes (:meth:`PendingChatInjection.loop_unreplied`).

Mirrors the :class:`teatree.core.models.deferred_question.DeferredQuestion`
shape — durable, single-use, scoped, idempotent — applied to the *reverse*
direction (user → agent). The Slack ``ts`` is the canonical idempotency
key: the scanner can over-poll safely because ``unique(overlay, ts)``
deduplicates.

``answered_at`` records that the agent personally replied (#1063). The
heuristic :attr:`is_question` lives here so the model is the single source of
truth for "this row needs a reply".
"""

import re
from dataclasses import dataclass
from functools import partial
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.telemetry.admission import record_lifecycle_transition

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

# Broad enough for the same question heuristic, but evaluated in SQL so a
# detector can LIMIT before materializing an unbounded inbound queue.
_QUESTION_TEXT_REGEX = (
    r"(?is)^[\s*_\->`#0-9.()]*"
    rf"(?:(?:{'|'.join(sorted(_QUESTION_WORDS))})(?![A-Za-z])"
    rf"|.*(?:{'|'.join(_QUESTION_PHRASES)})"
    r"|.*\?\s*$)"
)


@dataclass(frozen=True, slots=True)
class DmContext:
    """Who wrote an inbound DM and which thread it replies under.

    One value because they are used as one: together with ``(overlay, channel)``
    these are exactly the discriminators that decide which logical turn a
    message belongs to and which queued question a reply answers.
    """

    user_id: str = ""
    thread_ts: str = ""


NO_DM_CONTEXT = DmContext()


class PendingChatInjection(models.Model):
    """One Slack DM from the user, waiting for the reactive Slack-answer cycle.

    The scanner inserts a row per new message. ``answered_at`` is set
    set when the agent actually replies to the user (via
    :meth:`agent_answered_question` or the ``notify_user`` integration in
    :mod:`teatree.core.notify`). ``thread_ts`` is the Slack thread root a reply
    sits under, blank for a top-level DM — a reply's only binding identity,
    which :mod:`teatree.loop.question_binding` joins against a queued
    question's Slack mirror ts to answer the row the owner meant.
    """

    class AnswerKind(models.TextChoices):
        UNANSWERED = "", "Unanswered"
        ACK = "ack", "Ack"
        SIMPLE = "simple", "Simple"
        DELEGATED = "delegated", "Delegated"
        QUESTION_REPLY = "question_reply", "Question reply"

    question_text_regex: ClassVar[str] = _QUESTION_TEXT_REGEX

    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    thread_ts = models.CharField(max_length=64, blank=True, default="")
    user_id = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField()
    received_at = models.DateTimeField(default=timezone.now)
    # Set ONLY when the agent
    # personally replied to the user (via ``agent_answered_question`` or
    # the ``notify_user`` integration in :mod:`teatree.core.notify`).
    # The reactive Slack-answer loop must NOT write this column — see
    # ``loop_replied_at`` below (#1075).
    answered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # The loop's claim is separate from agent-personal ``answered_at`` (#1069/#1075).
    # The claim is a single-use compare-and-swap.
    loop_replied_at = models.DateTimeField(null=True, blank=True)
    # Written only after verified Slack readback, a successful in-flight
    # reaction API receipt, or a bound answer was applied. It proves response,
    # not completion of delegated work.
    loop_response_confirmed_at = models.DateTimeField(null=True, blank=True)
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
        status = "pending"
        if self.answered_at is not None:
            status = "answered"
        return f"pending-chat-injection<{self.pk}:{status} overlay={self.overlay!r} ts={self.slack_ts}>"

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
        context: DmContext = NO_DM_CONTEXT,
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
                "user_id": context.user_id,
                "text": text,
                "thread_ts": context.thread_ts,
            },
        )
        if created:
            transaction.on_commit(partial(record_lifecycle_transition, kind="message.received", entity_id=row.pk))
            return row
        return None

    @classmethod
    def latest_slack_ts(cls, *, overlay: str = "") -> str:
        """The newest ``slack_ts`` recorded for *overlay*, or ``""`` when none is.

        A Slack ``ts`` is a fixed-width epoch-seconds string, so the lexicographic
        max is the chronological one; ``""`` reads as "no cursor yet".
        """
        newest = cls.objects.filter(overlay=overlay).aggregate(newest=models.Max("slack_ts"))["newest"]
        return newest or ""

    @classmethod
    def loop_unreplied(cls, *, overlay: str = "") -> models.QuerySet["PendingChatInjection"]:
        """Return the reactive Slack-answer loop's work-queue, oldest first.

        Gates on ``loop_replied_at`` (the loop's own column, #1075 / Option B),
        NOT ``answered_at``: the loop posting a reply stamps only
        ``loop_replied_at``, so it never claims the agent personally answered.

        Pass ``overlay=""`` to scan every overlay's queue (the v1 single-
        overlay path uses ``overlay=""`` consistently).
        """
        qs = cls.objects.filter(loop_replied_at__isnull=True)
        if overlay:
            qs = qs.filter(overlay=overlay)
        return qs.order_by("received_at")

    def mark_loop_replied(self, kind: str) -> bool:
        """Stamp ``loop_replied_at`` + ``answer_kind``; ``True`` on the transition.

        Single-use compare-and-swap (``UPDATE … WHERE loop_replied_at IS
        NULL``): a concurrent second caller sees
        0 rows updated and returns ``False`` without overwriting the first
        ``answer_kind``. Writes ONLY the reactive Slack-answer loop's
        column (#1075 / Option B) — never ``answered_at`` ("the agent
        personally replied"). The loop replying must not claim that.
        """
        updated = (
            type(self)
            .objects.filter(pk=self.pk, loop_replied_at__isnull=True)
            .update(loop_replied_at=timezone.now(), answer_kind=kind)
        )
        if updated:
            self.refresh_from_db(fields=["loop_replied_at", "answer_kind"])
        return bool(updated)

    def observe_confirmed_loop_reply(self) -> None:
        """Persist verified delivery/application, distinct from the pre-post claim."""
        confirmed = (
            type(self)
            .objects.filter(
                pk=self.pk,
                loop_replied_at__isnull=False,
                loop_response_confirmed_at__isnull=True,
                answer_kind__in={self.AnswerKind.SIMPLE, self.AnswerKind.QUESTION_REPLY, self.AnswerKind.DELEGATED},
            )
            .update(loop_response_confirmed_at=timezone.now())
        )
        if confirmed:
            self.refresh_from_db(fields=["loop_response_confirmed_at"])
            transaction.on_commit(partial(record_lifecycle_transition, kind="message.answered", entity_id=self.pk))

    def unmark_loop_replied(self) -> bool:
        """Release the loop-reply claim; ``True`` if a stamp was cleared, else ``False``.

        The rollback half of :meth:`mark_loop_replied`: when the side-effect
        of a claimed loop reply (the ACK :white_check_mark: reaction) fails,
        the caller clears ``loop_replied_at`` + ``answer_kind`` so the unit
        re-enters ``loop_unreplied()`` and is retried next cycle instead of
        carrying a claim for a reply that never landed. A verified delivery
        receipt is never cleared, even if a later side effect fails.
        """
        updated = (
            type(self)
            .objects.filter(pk=self.pk, loop_replied_at__isnull=False, loop_response_confirmed_at__isnull=True)
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
        a ``t3:answerer`` Task), and ``answered_at``. The threaded reply IS the agent personally
        answering, so it satisfies both — unlike the cycle's own
        token-cheap reply, which deliberately stamps only ``loop_replied_at``.

        The two gates are stamped by their OWN single-use compare-and-swaps
        rather than one shared predicate. A row the cycle already loop-replied
        keeps its ``answer_kind`` (the cycle's token-cheap reply is not this
        personal answer), but its ``answered_at`` must still be stamped —
        gating both on ``loop_replied_at IS NULL`` left exactly that row
        unanswered forever. Returns the number of rows the ANSWERED
        gate transitioned. The empty ``thread_ts`` (a top-level DM, not a
        reply) matches nothing and returns ``0``.
        """
        if not thread_ts:
            return 0
        now = timezone.now()
        in_thread = cls.objects.filter(slack_ts=thread_ts)
        in_thread.filter(loop_replied_at__isnull=True).update(
            loop_replied_at=now,
            answer_kind=cls.AnswerKind.QUESTION_REPLY,
        )
        answered = int(in_thread.filter(answered_at__isnull=True).update(answered_at=now))
        if answered:
            row = in_thread.only("pk").first()
            if row is not None:
                transaction.on_commit(partial(record_lifecycle_transition, kind="message.answered", entity_id=row.pk))
        return answered

    @classmethod
    def agent_answered_question(cls, slack_ts: str) -> int:
        """Stamp ``answered_at = now`` on rows matching ``slack_ts``.

        Returns the number of rows actually transitioned from
        ``answered_at IS NULL`` to ``answered_at = now``. Idempotent: a
        second call on an already-answered row is a no-op and returns
        ``0``. The empty ``slack_ts`` is rejected — there is no row that
        the empty string could legitimately identify.

        The stamp is keyed on ``slack_ts`` alone. ``slack_ts`` is the unique idempotency key — a Slack message has
        exactly one ``ts`` per channel and the user has a single DM — so a
        single stamp keyed on it clears precisely that row and
        cannot cross-stamp another. This is what makes a concurrent multi-
        overlay deployment work: a session under one overlay answers a
        question recorded under a *different* overlay (the recording overlay
        and the answering session's ``T3_OVERLAY_NAME`` routinely differ).
        Scoping the stamp by overlay added no correctness — only the broken
        narrowing that stranded the row unanswered and nagged forever.
        """
        if not slack_ts:
            return 0
        answered = int(
            cls.objects.filter(slack_ts=slack_ts, answered_at__isnull=True).update(answered_at=timezone.now())
        )
        if answered:
            row = cls.objects.filter(slack_ts=slack_ts).only("pk").first()
            if row is not None:
                transaction.on_commit(partial(record_lifecycle_transition, kind="message.answered", entity_id=row.pk))
        return answered


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


__all__ = ["NO_DM_CONTEXT", "DmContext", "PendingChatInjection"]
