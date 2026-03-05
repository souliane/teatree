"""Tests for _registry.py — extension point registry."""

import pytest
from lib.registry import call, clear, get, register, registered_points


def _noop(**_kwargs: object) -> str:
    return "noop"


def _framework(**_kwargs: object) -> str:
    return "framework"


def _project(**_kwargs: object) -> str:
    return "project"


class TestRegister:
    def test_register_and_get(self) -> None:
        register("my_point", _noop, "default")
        assert get("my_point") is _noop

    def test_unregistered_returns_none(self) -> None:
        assert get("nonexistent") is None

    def test_higher_layer_wins(self) -> None:
        register("p", _noop, "default")
        register("p", _framework, "framework")
        assert get("p") is _framework

    def test_project_overrides_framework(self) -> None:
        register("p", _noop, "default")
        register("p", _framework, "framework")
        register("p", _project, "project")
        assert get("p") is _project

    def test_registering_at_same_layer_replaces(self) -> None:
        register("p", _noop, "default")
        register("p", _framework, "default")
        assert get("p") is _framework

    def test_invalid_layer_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown layer"):
            register("p", _noop, "bogus")

    def test_order_independent_of_registration_order(self) -> None:
        register("p", _project, "project")
        register("p", _noop, "default")
        assert get("p") is _project


class TestCall:
    def test_call_invokes_handler(self) -> None:
        register("greet", lambda name: f"hi {name}", "default")
        assert call("greet", "alice") == "hi alice"

    def test_call_passes_kwargs(self) -> None:
        register("greet", lambda name="world": f"hi {name}", "default")
        assert call("greet", name="bob") == "hi bob"

    def test_call_unregistered_raises_key_error(self) -> None:
        with pytest.raises(KeyError, match="No handler"):
            call("missing_point")

    def test_call_uses_highest_priority(self) -> None:
        register("p", lambda: "default", "default")
        register("p", lambda: "project", "project")
        assert call("p") == "project"


class TestClearAndPoints:
    def test_clear_empties_registry(self) -> None:
        register("a", _noop, "default")
        register("b", _noop, "default")
        clear()
        assert get("a") is None
        assert get("b") is None

    def test_registered_points(self) -> None:
        register("alpha", _noop, "default")
        register("beta", _noop, "default")
        points = registered_points()
        assert "alpha" in points
        assert "beta" in points
