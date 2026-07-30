"""Tests for the multi-tier timeout configuration system."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase, override_settings

from teatree import settings as teatree_settings
from teatree.timeouts import (
    CORE_DEFAULTS,
    DB_IMPORT,
    DOCKER_COMPOSE_BUILD,
    DOCKER_COMPOSE_DOWN,
    DOCKER_COMPOSE_UP,
    PRE_RUN_STEP,
    PROVISION_STEP,
    SETUP,
    START,
    TimeoutConfig,
    load_timeouts,
)


def _seed_config(db: Path, key: str, value: object, scope: str = "") -> None:
    """Seed a ``teatree_config_setting`` row the cold reader resolves."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            (scope, key, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


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

    def test_docker_compose_build_uses_600s_default(self) -> None:
        """First-time image builds need a long default, not the 120s fallback.

        Regression: ``docker_compose_build`` was missing from CORE_DEFAULTS,
        so ``get`` fell through to the hardcoded 120 for unknown operations —
        timing out first-time Gradle/Java sidecar builds at 120s.
        """
        cfg = TimeoutConfig()
        assert cfg.get(DOCKER_COMPOSE_BUILD) == 600
        assert CORE_DEFAULTS[DOCKER_COMPOSE_BUILD] == 600

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

    def test_user_db_setting_beats_overlay(self) -> None:
        class FakeOverlay:
            def get_timeouts(self) -> dict[str, int]:
                return {"setup": 500}

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "db.sqlite3"
            _seed_config(db, "timeouts", {"setup": 10})
            with mock.patch.dict(os.environ, {"T3_CONFIG_DB": str(db)}):
                cfg = load_timeouts(FakeOverlay())
        assert cfg.get(SETUP) == 10  # user DB setting wins over overlay

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
        for op in (
            SETUP,
            START,
            DB_IMPORT,
            DOCKER_COMPOSE_UP,
            DOCKER_COMPOSE_BUILD,
            DOCKER_COMPOSE_DOWN,
            PROVISION_STEP,
            PRE_RUN_STEP,
        ):
            val = cfg.get(op)
            assert val is not None, f"{op} should not be None"
            assert val > 0, f"{op} should have a positive default"


class TestTimeoutRegistryParity:
    """Fitness test binding the Django settings registry to CORE_DEFAULTS.

    The tier-3 core defaults have two surfaces: ``CORE_DEFAULTS`` in
    ``teatree.timeouts`` (the canonical, Django-free source) and
    ``TEATREE_TIMEOUTS`` in ``teatree.settings`` (the Django-settings
    surface that ``load_timeouts`` reads via ``django.conf.settings``).
    They must stay identical — same keys, same values. A key present in one
    registry but not the other silently falls to the 120s unknown-operation
    fallback in ``TimeoutConfig.get`` (the #2014 bug class). This binding
    makes that drift impossible.

    The assertion targets the production ``teatree.settings`` module
    directly, not the active ``django.conf.settings`` (the test harness
    swaps in ``tests.django_settings``, which intentionally omits the
    timeout registry).
    """

    def test_settings_registry_has_same_keys_as_core_defaults(self) -> None:
        assert set(teatree_settings.TEATREE_TIMEOUTS) == set(CORE_DEFAULTS)

    def test_settings_registry_equals_core_defaults(self) -> None:
        assert teatree_settings.TEATREE_TIMEOUTS == CORE_DEFAULTS
