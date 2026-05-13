"""Tests for teatree.core.apps — CoreConfig.ready() wires Django signals."""

import pytest
from django.apps import apps


def test_ready_registers_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ready()`` should call ``register_signals`` exactly once on import."""
    called: list[bool] = []

    def _fake_register() -> None:
        called.append(True)

    monkeypatch.setattr("teatree.core.signals.register_signals", _fake_register)

    apps.get_app_config("core").ready()

    assert called == [True]
