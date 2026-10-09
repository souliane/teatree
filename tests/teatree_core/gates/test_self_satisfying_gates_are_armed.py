# test-path: cross-cutting
"""Quality gates are structural rather than configurable."""

from dataclasses import fields

from teatree.config.settings import UserSettings


def test_retired_quality_switches_are_absent_from_settings() -> None:
    names = {field.name for field in fields(UserSettings)}
    for key in ("critic_gate_mode", "require_merge_quality_verdict", "require_merge_evidence"):
        assert key not in names
