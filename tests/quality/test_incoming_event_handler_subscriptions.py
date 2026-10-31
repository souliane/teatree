# test-path: cross-cutting
"""A handler registered for a GitHub event the hook never subscribes to would be dead code."""

from collections.abc import Iterable

from teatree.core.intake.github_payload import HOOK_EVENTS
from teatree.loop.scanners.event_handlers import HANDLERS, EventHandler, HandlerRegistry


def _unsubscribed(registry: HandlerRegistry, subscribed: Iterable[str]) -> frozenset[str]:
    return registry.events("github") - frozenset(subscribed)


def _registry_handling(*event_names: str) -> HandlerRegistry:
    registry = HandlerRegistry()
    for event_name in event_names:
        registry.register(EventHandler(source="github", event_name=event_name, loop="x", handle=lambda event: []))
    return registry


def test_every_registered_github_event_is_in_the_hook_subscription_list() -> None:
    assert not _unsubscribed(HANDLERS, HOOK_EVENTS)


def test_an_empty_registry_and_a_subscribed_event_pass() -> None:
    assert not _unsubscribed(_registry_handling(), HOOK_EVENTS)
    assert not _unsubscribed(_registry_handling("pull_request_review"), HOOK_EVENTS)


def test_an_event_the_hook_does_not_subscribe_to_fails() -> None:
    assert _unsubscribed(_registry_handling("pull_request_review", "fork"), HOOK_EVENTS) == {"fork"}
