"""The handler registry: one handler per fully-qualified (source, event name, action) triple."""

import pytest

from teatree.loop.scanners.event_handlers import EventHandler, HandlerRegistry


def _handler(*actions: str, source: str = "github", event_name: str = "pull_request_review") -> EventHandler:
    return EventHandler(
        source=source, event_name=event_name, loop="loop", handle=lambda event: [], actions=frozenset(actions)
    )


def test_lookup_finds_a_handler_only_for_its_exact_triple() -> None:
    registry = HandlerRegistry()
    handler = _handler("submitted")
    registry.register(handler)

    assert registry.lookup("github", "pull_request_review", "submitted") is handler
    assert registry.lookup("github", "pull_request_review", "dismissed") is None
    assert registry.lookup("github", "pull_request", "submitted") is None
    assert registry.lookup("gitlab", "pull_request_review", "submitted") is None


def test_a_handler_with_no_actions_takes_every_action() -> None:
    registry = HandlerRegistry()
    handler = _handler()
    registry.register(handler)

    assert registry.lookup("github", "pull_request_review", "") is handler
    assert registry.lookup("github", "pull_request_review", "anything") is handler


def test_a_specific_action_beats_the_any_action_handler() -> None:
    registry = HandlerRegistry()
    any_action, submitted = _handler(), _handler("submitted")
    registry.register(any_action)
    registry.register(submitted)

    assert registry.lookup("github", "pull_request_review", "submitted") is submitted
    assert registry.lookup("github", "pull_request_review", "edited") is any_action


@pytest.mark.parametrize(
    ("first", "second"),
    [(("submitted",), ("submitted",)), (("submitted", "edited"), ("edited",)), ((), ())],
)
def test_registering_the_same_triple_twice_raises(first: tuple[str, ...], second: tuple[str, ...]) -> None:
    registry = HandlerRegistry()
    registry.register(_handler(*first))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_handler(*second))


def test_a_refused_registration_leaves_the_registry_unchanged() -> None:
    registry = HandlerRegistry()
    registry.register(_handler("edited"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(_handler("submitted", "edited"))

    assert registry.lookup("github", "pull_request_review", "submitted") is None


def test_events_lists_the_event_names_registered_for_a_source() -> None:
    registry = HandlerRegistry()
    registry.register(_handler("submitted"))
    registry.register(_handler(event_name="issues"))
    registry.register(_handler(source="gitlab", event_name="note"))

    assert registry.events("github") == {"pull_request_review", "issues"}
    assert registry.events("gitlab") == {"note"}
    assert registry.events("slack") == frozenset()
    assert registry.keys() == {("github", "pull_request_review"), ("github", "issues"), ("gitlab", "note")}
