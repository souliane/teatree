# test-path: cross-cutting
"""The declared-key manifest pins every setting name across all families."""

import dataclasses
from pathlib import Path

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, SAFETY_POSTURE_KEYS, UserSettings
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.setting_taxonomy import SettingClass, classify, owner_only_reason
from teatree.mcp.write_tools import refuse_reason

_MANIFEST = Path(__file__).resolve().parents[2] / "src" / "teatree" / "config" / "declared_keys.txt"


def declared_manifest_keys() -> frozenset[str]:
    return frozenset(line for line in _MANIFEST.read_text(encoding="utf-8").splitlines() if line and " " not in line)


def test_manifest_matches_every_known_setting() -> None:
    assert declared_manifest_keys() == set(ALL_KNOWN_CONFIG_SETTINGS)


def test_user_fields_are_all_in_manifest() -> None:
    fields = {field.name for field in dataclasses.fields(UserSettings)}
    assert fields <= declared_manifest_keys()
    assert set(OVERLAY_OVERRIDABLE_SETTINGS) == fields


def test_forced_repro_setting_is_declared_and_owner_governed() -> None:
    key = "forced_repro_gate_enabled"
    assert UserSettings().forced_repro_gate_enabled is True
    assert key in OVERLAY_OVERRIDABLE_SETTINGS
    assert SettingClass.GATE_SWITCH in classify(key).classes
    assert owner_only_reason(key)
    assert refuse_reason(key)


def test_paid_route_fallback_requires_owner_authorization() -> None:
    key = "agent_route_paid_fallback"
    assert UserSettings().agent_route_paid_fallback is False
    assert key in OVERLAY_OVERRIDABLE_SETTINGS
    assert key in SAFETY_POSTURE_KEYS
    assert refuse_reason(key)
