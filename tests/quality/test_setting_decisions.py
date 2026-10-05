# test-path: cross-cutting — one contract over the whole settings surface
"""A shipped-off feature or gate has an explicit disposition."""

import dataclasses
import json
from pathlib import Path

from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.registries import COLD_HOOK_SETTINGS, COLD_SETTINGS
from teatree.config.settings import UserSettings

_MANIFEST = Path(__file__).parents[2] / "src" / "teatree" / "config" / "setting_decisions.json"


def _decisions() -> dict[str, dict[str, str]]:
    return json.loads(_MANIFEST.read_text())["decisions"]


def _off_features() -> set[str]:
    # Record every shipped-off bool. This deliberately includes durable operator
    # policies, so a new feature cannot hide behind an unconventional key name.
    fields = {field.name for field in dataclasses.fields(UserSettings) if field.default is False}
    cold = {key for key, spec in {**COLD_SETTINGS, **COLD_HOOK_SETTINGS}.items() if spec.default is False}
    return fields | cold


def _missing_decisions(off_features: set[str]) -> set[str]:
    return off_features - _decisions().keys()


def test_no_shipped_off_feature_or_gate_lacks_a_decision() -> None:
    assert not _missing_decisions(_off_features())


def test_every_decision_names_a_live_setting() -> None:
    names = {field.name for field in dataclasses.fields(UserSettings)} | ALL_KNOWN_CONFIG_SETTINGS.keys()
    assert not (_decisions().keys() - names)


def test_an_undecided_off_feature_is_caught() -> None:
    assert _missing_decisions(_off_features() | {"planted_shipped_off_enabled"}) == {"planted_shipped_off_enabled"}


def test_decisions_use_neutral_categories_without_tracker_ids() -> None:
    decisions = _decisions()
    assert {row["decision"] for row in decisions.values()} <= {"retain", "unified-gate"}
    assert all("OP-" not in row["reason"] for row in decisions.values())
