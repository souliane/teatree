"""Scanner that drains the ``IncomingEvent`` ingestion queue (#669).

Sits at the consumer end of the autonomous-events stack:

1. Reads ``IncomingEvent.objects.unprocessed()`` (limited per tick).
2. Classifies each via :func:`teatree.core.intake.intent_classifier.classify_event`.
3. Routes each via :func:`teatree.core.intake.event_router.route_event`.
4. Executes the side effect for the routed action.
5. Marks the event ``processed_at`` so it does not re-fire.

The first three steps are pure functions and were already covered by
``IntentClassification`` (#665) and the router (#667). This scanner is
the missing piece that actually drives them — without it the receivers
persist rows that sit forever.

The ``schedule_task`` and ``schedule_merge`` actions emit user-facing
``ScanSignal``s rather than auto-creating Tickets — automatic ticket
creation from inbound chat needs a separate decision pass on which
overlay owns the new ticket (the Source alone is not enough).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import OperationalError, ProgrammingError

import teatree.core.overlay_loader as _overlay_loader
from teatree.core.backend_protocols import MessagingBackend
from teatree.core.intake.event_router import RoutedAction, route_event
from teatree.core.intake.intent_classifier import classify_event
from teatree.core.reply_transport import NoopReplier, Replier
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models.incoming_event import IncomingEvent

logger = logging.getLogger(__name__)

type MessagingResolver = Callable[[str], MessagingBackend | None]


def _default_messaging_resolver(overlay: str) -> MessagingBackend | None:
    from teatree.core.backend_factory import messaging_from_overlay  # noqa: PLC0415

    return messaging_from_overlay(overlay or None)


def _ambient_directive_detection_enabled() -> bool:
    """Whether inbound DIRECTIVE events route to capture — the dark ``ambient_directive_detection_enabled`` (#116).

    Its OWN flag, NOT derived from ``directive_loop_enabled``: arming the explicit
    directive loop must never silently arm ambient detection of untrusted inbound
    content (the lethal-trifecta precondition). Resolved globally — an inbound Slack
    directive carries no forge URL to pick an overlay. Off (the default) keeps
    ``route_event`` dropping DIRECTIVE events exactly as an unrouteable intent.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — cross-layer import cycle

    return bool(get_effective_settings(None).ambient_directive_detection_enabled)


def _event_forge_url(event: "IncomingEvent") -> str:
    """Best-effort forge URL/slug for *event*, for overlay resolution.

    Prefers the merge request / pull request web URL carried in the webhook
    payload (GitLab ``object_attributes.url``, GitHub ``pull_request.html_url``),
    falling back to the ``owner/repo`` slug both receivers record as
    ``channel_ref`` (GitLab ``project.path_with_namespace``, GitHub
    ``repository.full_name``). Either shape is resolvable by
    :func:`teatree.core.overlay_loader.infer_overlay_for_url`. Returns ``""``
    when neither is present, so the caller falls back to the ambient
    single-overlay default (and fails loud on genuine ambiguity).
    """
    payload = event.payload_json or {}
    attrs = payload.get("object_attributes")
    if isinstance(attrs, dict):
        gitlab_url = attrs.get("url")
        if isinstance(gitlab_url, str) and gitlab_url:
            return gitlab_url
    pr = payload.get("pull_request")
    if isinstance(pr, dict):
        github_url = pr.get("html_url")
        if isinstance(github_url, str) and github_url:
            return github_url
    return event.channel_ref or ""


@dataclass(slots=True)
class IncomingEventsScanner:
    limit: int = 25
    name: str = "incoming_events"
    replier: Replier = field(default_factory=NoopReplier)
    messaging_resolver: MessagingResolver = field(default=_default_messaging_resolver)

    def scan(self) -> list[ScanSignal]:
        event_model = cast("type[IncomingEvent]", apps.get_model("core", "IncomingEvent"))
        try:
            # Materialise here so a present-but-un-migrated DB (the
            # `teatree_incoming_event` table doesn't exist yet on a
            # pre-migration install) is a silent no-op instead of a
            # per-tick WARN. Only the missing-relation errors are
            # swallowed — sqlite raises OperationalError "no such table",
            # Postgres raises ProgrammingError "relation does not exist".
            # Transient OperationalError (lock timeout, connection drop)
            # and any other DatabaseError keep propagating to
            # `tick._run_job`, which surfaces them on the statusline.
            events = list(event_model.objects.unprocessed().order_by("received_at", "pk")[: self.limit])
        except (OperationalError, ProgrammingError):
            logger.info("IncomingEventsScanner: teatree_incoming_event unavailable (DB not migrated yet) — skipping")
            return []
        signals: list[ScanSignal] = []
        for event in events:
            try:
                signal = self._handle(event)
            except Exception as exc:
                logger.exception("IncomingEventsScanner failed on event %s", event.pk)
                # Do NOT mark_processed: that silently drops the poison. Record
                # the failure so a transient error retries with backoff and a
                # persistent one dead-letters (surfaced, not queue-blocking).
                dead_lettered = event.record_failure(f"{type(exc).__name__}: {exc}")
                if dead_lettered:
                    signals.append(self._dead_letter_signal(event))
                continue
            event.mark_processed()
            if signal is not None:
                signals.append(signal)
        return signals

    @staticmethod
    def _dead_letter_signal(event: "IncomingEvent") -> ScanSignal:
        """Surface a poisoned event that exhausted its retries (#673 dead-letter view)."""
        return ScanSignal(
            kind="incoming_event.dead_letter",
            summary=f"dead-lettered {event.source} event after {event.attempts} attempts: {event.last_error}",
            payload={"event_id": event.pk, "source": event.source, "attempts": event.attempts},
        )

    def _handle(self, event: "IncomingEvent") -> ScanSignal | None:
        self._resolve_parent_text(event)
        classification = classify_event(event)
        action = route_event(
            event, classification, ambient_directive_detection_enabled=_ambient_directive_detection_enabled()
        )
        return self._execute(event, action)

    def _resolve_parent_text(self, event: "IncomingEvent") -> None:
        """Fetch and persist the parent message's text for a thread reply (#2230).

        The webhook records ``parent_ts`` deterministically (no network on
        the fast-return path) but a reply payload never carries the parent's
        text. Here in the loop — off the receiver's fast path — the parent
        text is fetched via the messaging backend and persisted so the
        answerer reads the referent ("approve posting the evidence?") rather
        than guessing from the bare reply. The backend resolves against the
        ambient single-overlay default (a Slack DM/channel event carries no
        forge URL to disambiguate); a backend-unavailable resolve, a missing
        message, or a raise leaves ``parent_text`` blank — the answerer still
        has ``parent_ts`` to read the thread itself.
        """
        if not event.is_thread_reply or event.parent_text:
            return
        backend = self.messaging_resolver("")
        if backend is None:
            return
        try:
            message = backend.fetch_message(channel=event.channel_ref, ts=event.parent_ts)
        except Exception as exc:  # noqa: BLE001 — a read raise must never block the queue
            logger.warning("Parent-text resolve raised for event %s: %s", event.pk, exc)
            return
        text = message.get("text") if isinstance(message, dict) else ""
        if isinstance(text, str) and text:
            event.record_parent_text(text)

    def _execute(self, event: "IncomingEvent", action: RoutedAction) -> ScanSignal | None:
        match action.kind:
            case RoutedAction.Kind.ALERT_USER:
                self.replier.post_dm(
                    event=event,
                    actor=action.target_ref,
                    body=action.detail,
                    idempotency_key=f"incoming:{event.idempotency_key}:alert",
                )
                return ScanSignal(
                    kind="incoming_event.alert",
                    summary=f"alert from {event.source}: {action.detail}",
                    payload={"event_id": event.pk, "actor": action.target_ref},
                )
            case RoutedAction.Kind.SCHEDULE_TASK:
                # `detail` carries the inbound body so the dispatcher can
                # spot a Slack review request (a PR/MR URL) and route it
                # to an independent review instead of a passive note (#219).
                # `parent_ts`/`parent_text` carry the replied-to message so
                # the answerer resolves the referent in context (#2230).
                return ScanSignal(
                    kind="incoming_event.task_needed",
                    summary=f"task request from {event.source} ({action.phase}): {action.detail}",
                    payload={
                        "event_id": event.pk,
                        "phase": action.phase,
                        "target_ref": action.target_ref,
                        "detail": action.detail,
                        "parent_ts": event.parent_ts,
                        "parent_text": event.parent_text,
                    },
                )
            case RoutedAction.Kind.SCHEDULE_MERGE:
                return self._handle_schedule_merge(event, action)
            case RoutedAction.Kind.RECORD_ONLY:
                return ScanSignal(
                    kind="incoming_event.recorded",
                    summary=f"status update from {event.source}",
                    payload={"event_id": event.pk, "target_ref": action.target_ref},
                )
            case RoutedAction.Kind.CAPTURE_DIRECTIVE:
                return self._capture_directive(event)
            case RoutedAction.Kind.DROP:
                return None

    @staticmethod
    def _capture_directive(event: "IncomingEvent") -> ScanSignal | None:
        """Capture an inbound DIRECTIVE event as a ``CAPTURED`` ``Directive`` (#63 path).

        Reached only when ``directive_loop_enabled`` gated ``route_event`` into a
        ``CAPTURE_DIRECTIVE`` action, so intake stays inert at default config. The
        directive is captured verbatim (the classifier only labels non-trivial text);
        a blank body is dropped rather than raising into the queue's dead-letter path.
        """
        from teatree.core.models import Directive  # noqa: PLC0415 — cross-layer import cycle

        if not (event.body or "").strip():
            return None
        directive = Directive.objects.capture(event.body, source=Directive.Source.INCOMING_EVENT, source_event=event)
        return ScanSignal(
            kind="incoming_event.directive_captured",
            summary=f"directive captured from {event.source}: {directive.raw_text[:60]}",
            payload={"event_id": event.pk, "directive_id": directive.pk},
        )

    @staticmethod
    def _handle_schedule_merge(event: "IncomingEvent", action: RoutedAction) -> ScanSignal:
        """Apply the overlay merge guard and return the appropriate signal.

        ``can_auto_merge`` is per-overlay policy (freeze windows, approval
        gates), so the guard must run against the overlay that owns the merge
        target — not a bare ``get_overlay()`` that raises ``Multiple overlays
        found`` in a multi-overlay install (souliane/teatree#1814 class). The
        owning overlay is resolved from the event's forge URL/slug
        (:func:`_event_forge_url`) which both webhook receivers record as the
        ``owner/repo`` ``channel_ref``; an unresolvable / ambiguous event
        falls through to ``get_overlay_for_url("")`` → ``get_overlay(None)``,
        which fails loud naming the installed overlays rather than picking one.
        """
        guard = _overlay_loader.get_overlay_for_url(_event_forge_url(event)).can_auto_merge(
            target_ref=action.target_ref,
            thread_ref=action.detail,
        )
        if guard.allowed:
            return ScanSignal(
                kind="incoming_event.merge_needed",
                summary=f"merge approved on {action.target_ref} ({action.detail})",
                payload={"event_id": event.pk, "target_ref": action.target_ref, "thread_ref": action.detail},
            )
        if guard.escalate:
            return ScanSignal(
                kind="incoming_event.merge_escalation",
                summary=f"merge escalation on {action.target_ref}: {guard.reason}",
                payload={
                    "event_id": event.pk,
                    "target_ref": action.target_ref,
                    "thread_ref": action.detail,
                    "reason": guard.reason,
                },
            )
        return ScanSignal(
            kind="incoming_event.merge_blocked",
            summary=f"merge blocked on {action.target_ref}: {guard.reason}",
            payload={
                "event_id": event.pk,
                "target_ref": action.target_ref,
                "thread_ref": action.detail,
                "reason": guard.reason,
            },
        )
