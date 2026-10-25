"""Scan Slack mentions and DMs drained from the Socket Mode JSONL queue."""

import logging
from dataclasses import dataclass, field

from teatree.core.backend_protocols import MessagingBackend
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def _ts(event: RawAPIDict) -> str:
    ts = event.get("ts") or event.get("event_ts")
    return ts if isinstance(ts, str) else ""


def _text(event: RawAPIDict) -> str:
    text = event.get("text")
    return text if isinstance(text, str) else ""


def _channel(event: RawAPIDict) -> str:
    channel = event.get("channel")
    return channel if isinstance(channel, str) else ""


def _resolve_permalink(backend: MessagingBackend, channel: str, ts: str) -> str:
    """Return ``backend.get_permalink`` result, or empty on any failure.

    The statusline renderer needs a clickable Slack URL but a transient
    Slack outage must not break statusline rendering — fall back to the
    bare ``ts`` label when the lookup fails (see #1050).
    """
    if not channel or not ts:
        return ""
    try:
        return backend.get_permalink(channel=channel, ts=ts)
    except Exception:  # noqa: BLE001 — permalink is decoration, never a hard failure
        return ""


@dataclass(slots=True)
class SlackMentionsScanner:
    """Surface ``app_mention`` and ``message.im`` events queued by Socket Mode.

    Side-effect for mentions referencing an MR/PR URL (#1047): record a
    :class:`ReviewAssignment` row and post the ``:eyes:`` reaction on the
    user's behalf via :func:`record_mention_intent`. Idempotent — the row
    is keyed on ``(overlay, mr_url, user_id)`` so re-firing on the same
    mention is a no-op. The existing ``slack.mention`` signal still routes
    to ``t3:reviewer`` via the dispatcher; the side effect just persists
    the ledger and acks the user.

    Each drained DM is also recorded (idempotently) as a ``PendingChatInjection``
    row, so a DM whose live record failed — the receiver writes the JSONL line
    first — still reaches the answer cycle without any polling.
    """

    backend: MessagingBackend
    overlay: str = ""
    name: str = "slack_mentions"
    _recorder: SlackDmRecorder | None = field(default=None, init=False, repr=False)

    def scan(self) -> list[ScanSignal]:
        mentions = self.backend.fetch_mentions()
        dms: list[RawAPIDict] = []

        drained_any, foreign, dm_records = self._drain_queue_into(mentions, dms)
        retry_events = self._record_dms(dms) if dms else []
        retry_ids = {id(event) for event in retry_events}

        signals: list[ScanSignal] = []
        for event in mentions:
            ts = _ts(event)
            signals.append(
                ScanSignal(
                    kind="slack.mention",
                    summary=f"Mention {ts}: {_text(event)[:80]}",
                    payload={"ts": ts, "event": event},
                )
            )
            self._record_review_intent(event)
        for event in dms:
            if id(event) in retry_ids:
                continue
            ts = _ts(event)
            channel = _channel(event)
            permalink = _resolve_permalink(self.backend, channel, ts)
            signals.append(
                ScanSignal(
                    kind="slack.dm",
                    summary=f"DM {ts}: {_text(event)[:80]}",
                    payload={"ts": ts, "event": event, "permalink": permalink},
                )
            )
        if drained_any:
            # Discard the backing file only after the DMs above are recorded. A
            # crash before this point leaves the ``.draining`` file for the next
            # drain to recover, so no mention is lost (Slack never retries them).
            from teatree.backends.slack.receiver import commit_drain  # noqa: PLC0415 — deferred: loaded at tick time

            commit_drain(retain=foreign + [record for record in dm_records if id(record.get("event")) in retry_ids])
        return signals

    def _drain_queue_into(
        self,
        mentions: list[RawAPIDict],
        dms: list[RawAPIDict],
    ) -> tuple[bool, list[dict], list[dict]]:
        """Fold this overlay's queued Socket Mode events into the mention/DM lists.

        Returns whether the JSONL queue yielded anything (so :meth:`scan`
        commits the backing file only after persisting) plus the records
        belonging to a SIBLING overlay, which :meth:`scan` hands back to the
        queue for that overlay's own scanner. ``reaction_added`` events land in
        ``slack-reactions.jsonl`` (drained by ``SlackReviewIntentScanner``,
        #1047) and never reach here.
        """
        from teatree.backends.slack.receiver import (  # noqa: PLC0415 — deferred: loaded at tick time
            drain_event_queue,
            record_is_owned_by,
        )

        drained_any = False
        foreign: list[dict] = []
        dm_records: list[dict] = []
        for queued in drain_event_queue():
            drained_any = True
            if not record_is_owned_by(queued, self.overlay):
                foreign.append(queued)
                continue
            event = queued.get("event", {})
            event_type = event.get("type", "")
            if event_type == "app_mention":
                mentions.append(event)
            elif event_type == "message" and event.get("channel_type", "") == "im":
                dms.append(event)
                dm_records.append(queued)
        return drained_any, foreign, dm_records

    def _record_dms(self, dms: list[RawAPIDict]) -> list[RawAPIDict]:
        if self._recorder is None:
            self._recorder = SlackDmRecorder(backend=self.backend, overlay=self.overlay)
        try:
            self._recorder.record_events(dms)
        except Exception:
            logger.exception("Failed to record %d drained DM(s)", len(dms))
            return dms
        return self._recorder.retry_events

    def _record_review_intent(self, event: RawAPIDict) -> None:
        """Persist a ``ReviewAssignment`` row and post ``:eyes:`` for MR-bearing mentions.

        Best-effort: any failure (DB unavailable in a test, Slack outage)
        logs and continues so the mention scanner never blocks on a
        side-effect. The maker/checker boundary (BLUEPRINT §17.8) is
        preserved because the actual review dispatch happens through the
        dispatcher's ``review_request_dispatch`` on the ``slack.mention``
        signal — this side-effect just persists the ledger and acks.
        """
        try:
            from teatree.loop.scanners.slack_review_intent import (  # noqa: PLC0415 — deferred: loaded at tick time, not import
                record_mention_intent,
            )

            record_mention_intent(event, backend=self.backend, overlay=self.overlay)
        except Exception:
            logger.exception("Failed to record review intent for mention %s", _ts(event))
