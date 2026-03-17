"""Tests for _registry.py — extension point registry."""

import pytest
from lib.registry import (
    MissingOverrideError,
    call,
    clear,
    get,
    info,
    register,
    registered_points,
    validate_overrides,
)


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

    def test_info_returns_sorted_entries(self) -> None:
        clear()
        register("zz_point", _noop, "default")
        register("aa_point", _framework, "framework")
        result = info()
        assert result[0]["point"] == "aa_point"
        assert result[1]["point"] == "zz_point"

    def test_info_shows_all_layers(self) -> None:
        clear()
        register("multi", _noop, "default")
        register("multi", _project, "project")
        result = info()
        assert len(result) == 1
        assert result[0]["active_layer"] == "project"
        assert "default" in result[0]["layers"]
        assert "project" in result[0]["layers"]

    def test_info_empty_registry(self) -> None:
        clear()
        assert info() == []


class TestValidateOverrides:
    def test_passes_when_project_layer_registered(self) -> None:
        register("wt_run_backend", _project, "project")
        register("wt_run_frontend", _project, "project")
        validate_overrides("services")  # should not raise

    def test_raises_when_only_default_layer(self) -> None:
        register("wt_run_backend", _noop, "default")
        register("wt_run_frontend", _noop, "default")
        with pytest.raises(MissingOverrideError, match="wt_run_backend"):
            validate_overrides("services")

    def test_raises_when_framework_but_not_project(self) -> None:
        register("wt_run_backend", _framework, "framework")
        register("wt_run_frontend", _project, "project")
        with pytest.raises(MissingOverrideError, match="wt_run_backend"):
            validate_overrides("services")

    def test_message_includes_pythonpath_hint(self) -> None:
        register("wt_run_backend", _noop, "default")
        register("wt_run_frontend", _noop, "default")
        with pytest.raises(MissingOverrideError, match="PYTHONPATH"):
            validate_overrides("services")

    def test_db_phase_checks_db_points(self) -> None:
        register("wt_db_import", _noop, "default")
        register("wt_post_db", _noop, "default")
        with pytest.raises(MissingOverrideError, match="wt_db_import"):
            validate_overrides("db")

    def test_raises_when_point_not_registered_at_all(self) -> None:
        # wt_run_backend/frontend not registered at all
        with pytest.raises(MissingOverrideError, match="not registered"):
            validate_overrides("services")
