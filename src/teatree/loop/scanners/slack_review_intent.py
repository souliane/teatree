"""Reaction-driven review auto-assign + emoji feedback loop (#1047).

The scanner watches Slack ``reaction_added`` and ``app_mention`` events for
the user-id configured on the messaging backend. When the underlying message
references an MR/PR URL, the scanner:

* persists one :class:`ReviewAssignment` row idempotent on
    ``(overlay, mr_url, user_id)`` — the canonical "user wants to review this"
    ledger;
* emits a single ``slack.review_intent`` signal that the dispatcher routes
    to the ``t3:reviewer`` agent — the maker/checker boundary is preserved
    because the reviewer agent runs as a separate dispatch from whatever
    produced the Slack message.

No claim reaction is posted at discovery time. A ``:eyes:`` reaction is a
*claim* on a colleague's review and must appear only when a review is DONE,
never the moment t3 first sees the request (#113/#86). The review-intent
signals are also passed through :func:`teatree.loop.review_claim_signals.filter_review_intent_signals`
so a *stopped* review loop queues none of them (#79).

Mirrors :class:`teatree.loop.scanners.slack_dm_inbound.SlackDmInboundScanner`:
durable idempotent persistence, single-signal emission, no agent
invocation here (the dispatcher routes signals to agents). The
``approve_review_assignment`` helper closes the loop when an MR the user
reviewed is approved by t3 — it advances ledger rows to ``approved`` so
the audit trail captures the full reaction → review → approval cycle.
The ``:white_check_mark:`` Slack reaction itself is posted by
``add_approval_reaction`` on the ``PullRequest.approve`` transition (see
``teatree.core.signals``) — the review-DONE outcome reaction.
"""

import logging
from dataclasses import dataclass
from typing import cast

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import ReviewAssignment, ReviewIntent
from teatree.loop.review_claim_signals import filter_review_intent_signals
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict
from teatree.url_classify import first_pr_url

logger = logging.getLogger(__name__)


def _first_mr_url(text: str) -> str:
    return first_pr_url(text)


def _event_user(event: RawAPIDict) -> str:
    user = event.get("user")
    return user if isinstance(user, str) else ""


def _reaction_item(event: RawAPIDict) -> tuple[str, str]:
    raw = event.get("item")
    if not isinstance(raw, dict):
        return "", ""
    item = cast("RawAPIDict", raw)
    channel = item.get("channel")
    ts = item.get("ts")
    return (
        channel if isinstance(channel, str) else "",
        ts if isinstance(ts, str) else "",
    )


@dataclass(slots=True)
class SlackReviewIntentScanner:
    """Translate Slack reaction/mention triggers into ``ReviewAssignment`` rows.

    *overlay* tags rows so a multi-overlay deployment can dispatch per
    overlay; v1 single-overlay use sets ``overlay=""``. The scanner is
    safe to over-poll because rows are keyed on
    ``(overlay, mr_url, user_id)``.
    """

    backend: MessagingBackend
    overlay: str = ""
    name: str = "slack_review_intent"

    def scan(self) -> list[ScanSignal]:
        target_user = getattr(self.backend, "user_id", "")
        signals: list[ScanSignal] = []

        reactions, drained_file = self._drain_reactions()
        for event in reactions:
            ts = event.get("ts") or event.get("event_ts", "<unknown>")
            try:
                signal = self._handle_reaction(event, target_user)
            except Exception:
                logger.exception("SlackReviewIntentScanner failed on reaction event %s", ts)
                continue
            if signal is not None:
                signals.append(signal)
        if drained_file:
            # Discard the backing file only after the reactions above are
            # handled (rows persisted) — a crash before this point leaves it
            # for the next drain to recover (#1047).
            from teatree.backends.slack.receiver import commit_reactions_drain  # noqa: PLC0415 — tick-time import

            commit_reactions_drain()

        for event in self._drain_mentions():
            ts = event.get("ts") or event.get("event_ts", "<unknown>")
            try:
                signal = self._handle_mention(event, target_user)
            except Exception:
                logger.exception("SlackReviewIntentScanner failed on mention event %s", ts)
                continue
            if signal is not None:
                signals.append(signal)

        # #79: a review-intent dispatch is a claim on a colleague's review;
        # when the review loop is stopped the loop must queue none of them.
        return filter_review_intent_signals(signals)

    def _drain_reactions(self) -> tuple[list[RawAPIDict], bool]:
        """Drain reaction events; flag whether the file-backed queue had any.

        Production path: pop from ``slack-reactions.jsonl`` via
        :func:`drain_reactions_queue`. Test path: pop from the backend's
        in-memory ``fetch_reactions`` (used by ``FakeMessaging`` so unit
        tests stay file-system free). The returned flag is true when the
        JSONL queue yielded events, so :meth:`scan` knows to commit the
        backing file only after the rows are persisted.
        """
        from teatree.backends.slack.receiver import drain_reactions_queue  # noqa: PLC0415 — tick-time import

        events: list[RawAPIDict] = []
        drained_file = False
        for queued in drain_reactions_queue():
            drained_file = True
            event = queued.get("event", {})
            if isinstance(event, dict):
                events.append(event)
        fetch_reactions = getattr(self.backend, "fetch_reactions", None)
        if callable(fetch_reactions):
            events.extend(fetch_reactions())
        return events, drained_file

    def _drain_mentions(self) -> list[RawAPIDict]:
        """Drain mention events without consuming the JSONL queue.

        ``SlackMentionsScanner`` owns the JSONL drain for mentions — running
        a second drain here would race the file rename. We read mentions
        from the backend's in-memory queue instead (populated either by
        Socket Mode ``enqueue_mention`` or by a test fake). For mentions
        already drained by ``SlackMentionsScanner`` into ``slack.mention``
        signals, the dispatcher's existing ``review_request_dispatch``
        path still routes them to ``t3:reviewer`` — this scanner just adds
        the persistence layer + ``:eyes:`` post for the cases where the
        mention reaches us first (test path or stand-alone deployment).
        """
        fetch_mentions = getattr(self.backend, "fetch_mentions", None)
        if not callable(fetch_mentions):
            return []
        return list(fetch_mentions())

    def _handle_reaction(self, event: RawAPIDict, target_user: str) -> ScanSignal | None:
        user = _event_user(event)
        if not target_user or user != target_user:
            return None
        channel, ts = _reaction_item(event)
        if not channel or not ts:
            return None
        message = self.backend.fetch_message(channel=channel, ts=ts)
        text = message.get("text") if isinstance(message, dict) else ""
        if not isinstance(text, str):
            return None
        mr_url = _first_mr_url(text)
        if not mr_url:
            return None
        intent = ReviewIntent(
            mr_url=mr_url,
            user_id=user,
            channel=channel,
            slack_ts=ts,
            trigger=ReviewAssignment.Trigger.REACTION,
            overlay=self.overlay,
        )
        row = ReviewAssignment.record(intent)
        if row is None:
            # Already observed this (overlay, mr_url, user_id) — no
            # double dispatch.
            return None
        return _signal_from_row(row, overlay=self.overlay)

    def _handle_mention(self, event: RawAPIDict, target_user: str) -> ScanSignal | None:
        _ = target_user  # ``record_mention_intent`` resolves the user from the backend.
        row = record_mention_intent(event, backend=self.backend, overlay=self.overlay)
        if row is None:
            return None
        return _signal_from_row(row, overlay=self.overlay)


def _signal_from_row(row: ReviewAssignment, *, overlay: str) -> ScanSignal:
    return ScanSignal(
        kind="slack.review_intent",
        summary=f"Review intent ({row.trigger}): {row.mr_url}",
        payload={
            "url": row.mr_url,
            "mr_url": row.mr_url,
            "user_id": row.user_id,
            "channel": row.channel,
            "ts": row.slack_ts,
            "trigger": row.trigger,
            "overlay": overlay,
        },
    )


def record_mention_intent(
    event: RawAPIDict,
    *,
    backend: MessagingBackend,
    overlay: str = "",
) -> ReviewAssignment | None:
    """Persist a :class:`ReviewAssignment` row for an MR-bearing mention.

    Called from :class:`SlackMentionsScanner` as a side-effect during the
    mention drain. Returns the new row on first observation, ``None`` when
    the mention has no MR URL or the row already existed.

    No ``:eyes:`` reaction is posted here: a claim reaction must appear only
    when a review is DONE, never at the moment a mention is first observed
    (#113/#86). The row records the *intent* to review; the FSM transition
    path posts the outcome reaction once a review actually lands.
    """
    raw_text = event.get("text")
    raw_channel = event.get("channel")
    raw_ts = event.get("ts") or event.get("event_ts")
    if not isinstance(raw_text, str) or not isinstance(raw_channel, str) or not isinstance(raw_ts, str):
        return None
    if not raw_text or not raw_channel or not raw_ts:
        return None
    mr_url = _first_mr_url(raw_text)
    if not mr_url:
        return None
    user_id = getattr(backend, "user_id", "")
    if not user_id:
        return None
    intent = ReviewIntent(
        mr_url=mr_url,
        user_id=user_id,
        channel=raw_channel,
        slack_ts=raw_ts,
        trigger=ReviewAssignment.Trigger.MENTION,
        overlay=overlay,
    )
    return ReviewAssignment.record(intent)


def approve_review_assignment(*, mr_url: str, overlay: str = "") -> int:
    """Compatibility wrapper around :meth:`ReviewAssignment.approve_for_mr`.

    The canonical implementation lives on the model so ``teatree.core``
    can call it without an arch-layer violation (core → loop is
    forbidden). This module-level alias keeps a stable entry point for
    callers reading from the loop layer.
    """
    return ReviewAssignment.approve_for_mr(mr_url=mr_url, overlay=overlay)
