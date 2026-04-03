"""Tests for the multi-tier timeout configuration system."""

from unittest.mock import patch

import pytest
from django.test import TestCase, override_settings

from teatree.timeouts import (
    CORE_DEFAULTS,
    DB_IMPORT,
    DOCKER_COMPOSE_DOWN,
    DOCKER_COMPOSE_UP,
    PRE_RUN_STEP,
    PROVISION_STEP,
    SETUP,
    START,
    TimeoutConfig,
    load_timeouts,
)


class TestTimeoutConfig:
    def test_defaults(self) -> None:
        cfg = TimeoutConfig()
        assert cfg.get(SETUP) == 120
        assert cfg.get(START) == 60
        assert cfg.get(DB_IMPORT) == 180

    def test_custom_values(self) -> None:
        cfg = TimeoutConfig(values={SETUP: 300, START: 90})
        assert cfg.get(SETUP) == 300
        assert cfg.get(START) == 90

    def test_zero_disables_timeout(self) -> None:
        cfg = TimeoutConfig(values={SETUP: 0})
        assert cfg.get(SETUP) is None

    def test_unknown_operation_uses_default(self) -> None:
        cfg = TimeoutConfig(values={})
        assert cfg.get(SETUP) == CORE_DEFAULTS[SETUP]

    def test_completely_unknown_operation_returns_120(self) -> None:
        cfg = TimeoutConfig(values={})
        assert cfg.get("nonexistent_operation") == 120

    def test_frozen(self) -> None:
        cfg = TimeoutConfig()
        with pytest.raises(AttributeError):
            cfg.values = {}  # type: ignore[misc]


class TestLoadTimeouts(TestCase):
    def test_core_defaults(self) -> None:
        cfg = load_timeouts()
        for op, default in CORE_DEFAULTS.items():
            assert cfg.get(op) == default

    @override_settings(TEATREE_TIMEOUTS={"setup": 999, "db_import": 0})
    def test_django_settings_override(self) -> None:
        cfg = load_timeouts()
        assert cfg.get(SETUP) == 999
        assert cfg.get(DB_IMPORT) is None  # 0 disables

    def test_overlay_overrides_django_settings(self) -> None:
        class FakeOverlay:
            def get_timeouts(self) -> dict[str, int]:
                return {"setup": 500, "provision_step": 300}

        cfg = load_timeouts(FakeOverlay())
        assert cfg.get(SETUP) == 500
        assert cfg.get(PROVISION_STEP) == 300
        # Non-overridden values use core defaults
        assert cfg.get(DOCKER_COMPOSE_UP) == CORE_DEFAULTS[DOCKER_COMPOSE_UP]

    @override_settings(TEATREE_TIMEOUTS={"setup": 999})
    def test_overlay_beats_django_settings(self) -> None:
        class FakeOverlay:
            def get_timeouts(self) -> dict[str, int]:
                return {"setup": 500}

        cfg = load_timeouts(FakeOverlay())
        assert cfg.get(SETUP) == 500  # overlay wins over Django settings

    def test_user_toml_beats_overlay(self) -> None:
        class FakeOverlay:
            def get_timeouts(self) -> dict[str, int]:
                return {"setup": 500}

        fake_config_raw = {"teatree": {"timeouts": {"setup": 10}}}

        class FakeConfig:
            raw = fake_config_raw

        with patch("teatree.config.load_config", return_value=FakeConfig()):
            cfg = load_timeouts(FakeOverlay())
        assert cfg.get(SETUP) == 10  # user TOML wins over overlay

    def test_overlay_without_get_timeouts(self) -> None:
        """Overlay that doesn't implement get_timeouts() is fine."""

        class BareOverlay:
            pass

        cfg = load_timeouts(BareOverlay())
        assert cfg.get(SETUP) == CORE_DEFAULTS[SETUP]

    def test_overlay_returning_empty(self) -> None:
        class EmptyOverlay:
            def get_timeouts(self) -> dict[str, int]:
                return {}

        cfg = load_timeouts(EmptyOverlay())
        assert cfg.get(SETUP) == CORE_DEFAULTS[SETUP]

    def test_all_operations_have_defaults(self) -> None:
        cfg = load_timeouts()
        for op in (SETUP, START, DB_IMPORT, DOCKER_COMPOSE_UP, DOCKER_COMPOSE_DOWN, PROVISION_STEP, PRE_RUN_STEP):
            val = cfg.get(op)
            assert val is not None, f"{op} should not be None"
            assert val > 0, f"{op} should have a positive default"
