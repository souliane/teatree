"""Tests for the finite state machine decorator."""

import pytest
from lib.fsm import (
    ConditionFailedError,
    InvalidTransitionError,
    generate_mermaid,
    get_available_transitions,
    get_transitions,
    transition,
)


def _always_true(_self: object) -> bool:
    return True


def _always_false(_self: object) -> bool:
    return False


class FakeLifecycle:
    state: str = "idle"

    def save(self) -> None:
        """No-op persistence for testing."""

    @transition(source="idle", target="active", conditions=[_always_true])
    def activate(self) -> str:
        return "activated"

    @transition(source="active", target="idle")
    def deactivate(self) -> None:
        pass

    @transition(source="idle", target="blocked", conditions=[_always_false])
    def block(self) -> None:
        pass

    @transition(source=["active", "idle"], target="done")
    def finish(self) -> None:
        pass

    @transition(source="*", target="idle")
    def reset(self) -> None:
        pass


class NoSaveLifecycle:
    """Lifecycle without a save method — tests that save is optional."""

    state: str = "idle"

    @transition(source="idle", target="active")
    def activate(self) -> None:
        pass


class TestTransitionDecorator:
    def test_valid_transition(self) -> None:
        obj = FakeLifecycle()
        result = obj.activate()
        assert result == "activated"
        assert obj.state == "active"

    def test_invalid_source_state(self) -> None:
        obj = FakeLifecycle()
        with pytest.raises(InvalidTransitionError, match="Cannot deactivate from idle"):
            obj.deactivate()

    def test_condition_blocks_transition(self) -> None:
        obj = FakeLifecycle()
        with pytest.raises(ConditionFailedError, match="_always_false"):
            obj.block()
        assert obj.state == "idle"  # state unchanged

    def test_multi_source_states(self) -> None:
        obj = FakeLifecycle()
        obj.finish()
        assert obj.state == "done"

    def test_wildcard_source(self) -> None:
        obj = FakeLifecycle()
        obj.state = "anything"
        obj.reset()
        assert obj.state == "idle"

    def test_fsm_metadata_on_method(self) -> None:
        assert hasattr(FakeLifecycle.activate, "_fsm")
        meta = FakeLifecycle.activate._fsm
        assert meta["source"] == ["idle"]
        assert meta["target"] == "active"
        assert len(meta["conditions"]) == 1

    def test_save_not_required(self) -> None:
        obj = NoSaveLifecycle()
        obj.activate()
        assert obj.state == "active"

    def test_transition_with_args(self) -> None:
        """Ensure extra positional and keyword args are forwarded."""

        class ArgsLifecycle:
            state: str = "a"

            @transition(source="a", target="b")
            def go(self, value: int, label: str = "default") -> str:
                return f"{value}-{label}"

        obj = ArgsLifecycle()
        result = obj.go(42, label="custom")
        assert result == "42-custom"
        assert obj.state == "b"


class TestIntrospection:
    def test_get_all_transitions(self) -> None:
        transitions = get_transitions(FakeLifecycle)
        names = [t["method"] for t in transitions]
        assert "activate" in names
        assert "deactivate" in names
        assert "reset" in names

    def test_get_available_transitions(self) -> None:
        obj = FakeLifecycle()
        available = get_available_transitions(obj)
        names = [t["method"] for t in available]
        assert "activate" in names
        assert "finish" in names
        assert "reset" in names  # wildcard is always available
        assert "deactivate" not in names  # source is "active", not "idle"

    def test_get_available_transitions_after_transition(self) -> None:
        obj = FakeLifecycle()
        obj.activate()
        available = get_available_transitions(obj)
        names = [t["method"] for t in available]
        assert "deactivate" in names
        assert "finish" in names
        assert "activate" not in names

    def test_generate_mermaid(self) -> None:
        mermaid = generate_mermaid(FakeLifecycle)
        assert "stateDiagram-v2" in mermaid
        assert "idle --> active" in mermaid
        assert "activate" in mermaid

    def test_generate_mermaid_wildcard_expands(self) -> None:
        mermaid = generate_mermaid(FakeLifecycle)
        # Wildcard reset should expand to all known states
        assert "active --> idle : reset" in mermaid
        assert "blocked --> idle : reset" in mermaid
        assert "done --> idle : reset" in mermaid

    def test_generate_mermaid_conditions_shown(self) -> None:
        mermaid = generate_mermaid(FakeLifecycle)
        assert "activate [_always_true]" in mermaid
        assert "block [_always_false]" in mermaid
