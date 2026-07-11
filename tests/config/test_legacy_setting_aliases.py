# test-path: cross-cutting
"""The retired-key alias contract for DB-home settings (souliane/teatree#3109).

A DB-home ``UserSettings`` field renamed without an alias (or a data migration)
silently discards any ``ConfigSetting`` row stored under the old key: the row
falls through ``_coerce_db_rows`` and the field takes its dataclass default. The
``speed`` -> ``wip`` rename (#2951) was exactly that — an install that set
``speed = full`` silently ran at ``Wip.MEDIUM``.

Two guards live here:

*   the instance guard — a stored ``speed`` row resolves to ``Wip.FULL`` through
    the ``speed -> wip`` alias;
*   the class-of-bug guard — every key that has ever been a DB-home settings
    field name (the explicitly-maintained ``_RETIRED_SETTING_KEYS`` registry) is
    still reachable: a current ``UserSettings`` field or a ``_LEGACY_SETTING_ALIASES``
    entry, so the next rename cannot silently drop an operator's setting.

Integration-first per the Test-Writing Doctrine: real ``ConfigSetting`` rows
resolved through ``get_effective_settings``.
"""

import dataclasses

import pytest
from django.test import TestCase

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, Autonomy, UserSettings, Wip, get_effective_settings
from teatree.config import resolution as _resolution
from teatree.config.resolution import _LEGACY_SETTING_ALIASES, _RETIRED_SETTING_KEYS
from teatree.core.models import ConfigSetting


class RetiredSpeedRowResolvesToWip(TestCase):
    """A ``ConfigSetting`` row under the retired ``speed`` key still takes effect."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_WIP", raising=False)

    def test_stored_speed_full_resolves_to_wip_full(self) -> None:
        # The bug: on main this resolves Wip.MEDIUM because `speed` has no alias.
        ConfigSetting.objects.set_value("speed", "full")
        assert get_effective_settings().wip is Wip.FULL

    def test_stored_speed_slow_resolves_to_wip_slow(self) -> None:
        ConfigSetting.objects.set_value("speed", "slow")
        assert get_effective_settings().wip is Wip.SLOW

    def test_canonical_wip_row_wins_over_retired_speed_row(self) -> None:
        # The alias only fills a gap: a current `wip` row always beats a `speed` row.
        ConfigSetting.objects.set_value("speed", "boost")
        ConfigSetting.objects.set_value("wip", "slow")
        assert get_effective_settings().wip is Wip.SLOW


class AliasFoldedGatePinSurvivesAutonomyCollapse(TestCase):
    """A global approval-gate row stored under a RETIRED alias still pins the gate (config §3d #1).

    ``get_effective_settings`` folds a legacy-alias row's VALUE onto its current
    field via ``_coerce_db_rows`` — but the autonomy-collapse pin set must be keyed
    off those FOLDED field names, not the raw row keys. On the buggy code the pin
    set was ``set(global_rows)`` (raw keys), so an approval gate stored under a
    renamed key resolved its value while its pin vanished: the ``full``/``notify``
    collapse then overrode the operator's explicitly-stored gate. This is the
    RED-at-HEAD proof; the fix keys the pin set off ``global_db`` (folded names).
    """

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_MODE", "T3_WIP"):
            monkeypatch.delenv(env, raising=False)
        # Simulate a future rename of an approval gate: the row is stored under the
        # old key and folded onto the current field on read.
        monkeypatch.setattr(
            _resolution,
            "_LEGACY_SETTING_ALIASES",
            {**_LEGACY_SETTING_ALIASES, "legacy_merge_gate": "require_human_approval_to_merge"},
        )

    def test_aliased_global_gate_row_survives_full_autonomy(self) -> None:
        # Operator explicitly kept the merge gate ON, but under the OLD key name;
        # the full-autonomy collapse must NOT relax it back to False.
        ConfigSetting.objects.set_value("autonomy", "full")
        ConfigSetting.objects.set_value("legacy_merge_gate", value=True)
        settings = get_effective_settings()
        assert settings.autonomy is Autonomy.FULL
        assert settings.require_human_approval_to_merge is True

    def test_canonical_row_under_new_key_also_pins(self) -> None:
        # The non-aliased control: a row under the current key pins identically.
        ConfigSetting.objects.set_value("autonomy", "full")
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=True)
        assert get_effective_settings().require_human_approval_to_merge is True


class RetiredSettingKeysStayReachable(TestCase):
    """The class-of-bug guard: a renamed DB-home key must never silently drop rows.

    ``_RETIRED_SETTING_KEYS`` is the explicitly-maintained record of every key that
    has ever been a DB-home settings field name. Retiring a key is a deliberate
    two-part edit (record it here, wire its alias), and this guard fails loudly if
    the alias half is missing.
    """

    @staticmethod
    def _current_fields() -> set[str]:
        return {field.name for field in dataclasses.fields(UserSettings)}

    def test_every_retired_key_is_a_current_field_or_aliased(self) -> None:
        current = self._current_fields()
        unresolved = sorted(
            key for key in _RETIRED_SETTING_KEYS if key not in current and key not in _LEGACY_SETTING_ALIASES
        )
        assert not unresolved, (
            f"Retired DB-home settings key(s) {unresolved} are neither a current UserSettings "
            f"field nor in _LEGACY_SETTING_ALIASES, so a ConfigSetting row stored under the old "
            f"key is silently ignored. For each key, either add "
            f"'<old_key>': '<current_field>' to _LEGACY_SETTING_ALIASES in "
            f"src/teatree/config/resolution.py, or ship a data migration that renames the "
            f"stored rows and drop the key from _RETIRED_SETTING_KEYS."
        )

    def test_every_alias_key_is_recorded_as_retired(self) -> None:
        # The registry is the canonical record of renames: an alias without a
        # retired-key entry means the guard above cannot see it.
        unrecorded = sorted(set(_LEGACY_SETTING_ALIASES) - set(_RETIRED_SETTING_KEYS))
        assert not unrecorded, (
            f"_LEGACY_SETTING_ALIASES key(s) {unrecorded} are missing from _RETIRED_SETTING_KEYS. "
            f"Add each retired key to _RETIRED_SETTING_KEYS so the reachability guard covers it."
        )

    def test_every_alias_target_actually_resolves(self) -> None:
        # A dangling alias (target absent from the DB-home parser registry) would
        # silently drop the row exactly like a missing alias.
        dangling = sorted(
            f"{old} -> {new}" for old, new in _LEGACY_SETTING_ALIASES.items() if new not in OVERLAY_OVERRIDABLE_SETTINGS
        )
        assert not dangling, (
            f"_LEGACY_SETTING_ALIASES target(s) {dangling} are not in OVERLAY_OVERRIDABLE_SETTINGS, "
            f"so a row under the old key resolves to nothing. Point the alias at a DB-home field."
        )
