from collections.abc import Callable
from contextlib import AbstractContextManager
from unittest.mock import patch

from teatree.core.models import IncomingEvent, Loop, Mode, ModeOverride, Prompt
from teatree.loop.scanners import event_handlers
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.event_handlers import EventHandler, HandlerRegistry

STUB_LOOP = "stub-forge-events"
ISSUE_URL = "https://github.com/acme/widgets/issues/7"
TASKS_SETTING = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}

type Handle = Callable[[IncomingEvent], list[ScanSignal]]


def starts_ticket(event: IncomingEvent) -> list[ScanSignal]:
    return [
        ScanSignal(
            kind="issue_intake.admitted",
            summary=f"stub handled {event.idempotency_key}",
            payload={"issue_url": ISSUE_URL, "auto_start": True, "overlay": ""},
        )
    ]


def raises(event: IncomingEvent) -> list[ScanSignal]:
    msg = f"stub failure on {event.idempotency_key}"
    raise RuntimeError(msg)


def registry_of(*handlers: EventHandler) -> AbstractContextManager[HandlerRegistry]:
    registry = HandlerRegistry()
    for handler in handlers:
        registry.register(handler)
    return patch.object(event_handlers, "HANDLERS", registry)


def review_handler(handle: Handle = starts_ticket, *, loop: str = STUB_LOOP) -> EventHandler:
    return EventHandler(
        source="github",
        event_name="pull_request_review",
        actions=frozenset({"submitted"}),
        loop=loop,
        handle=handle,
    )


def issues_handler(handle: Handle = starts_ticket, *, loop: str = STUB_LOOP) -> EventHandler:
    return EventHandler(source="github", event_name="issues", loop=loop, handle=handle)


def registered(handle: Handle = starts_ticket, *, loop: str = STUB_LOOP) -> AbstractContextManager[HandlerRegistry]:
    return registry_of(review_handler(handle, loop=loop))


def loop_row(name: str = STUB_LOOP) -> Loop:
    prompt, _ = Prompt.objects.get_or_create(name=f"{name}-prompt", defaults={"body": "x"})
    return Loop.objects.update_or_create(
        name=name, defaults={"delay_seconds": 60, "prompt": prompt, "script": "", "enabled": None}
    )[0]


def preset_admitting(name: str = STUB_LOOP, *, admitted: bool) -> None:
    Mode.objects.update_or_create(name="stub-preset", defaults={"entries": {name: admitted}})
    ModeOverride.objects.set_override("stub-preset", reason="test override")


def pending_review_event(key: str = "github:d-1", **fields: object) -> IncomingEvent:
    defaults: dict[str, object] = {
        "source": IncomingEvent.Source.GITHUB,
        "event_name": "pull_request_review",
        "action": "submitted",
        "origin": IncomingEvent.Origin.WEBHOOK,
        "repo_key": "github:101",
        "body": "approved",
        "payload_json": {"review": {"state": "approved"}},
        "idempotency_key": key,
    }
    return IncomingEvent.objects.create(**(defaults | fields))
