# test-path: cross-cutting
"""``COLD_HOOK_SETTINGS`` registers every pre-Django cold-hook setting (config-unify PR2).

The no-silent-drop fitness guard. The cold hook layer reads a set of global
``[teatree]`` keys before any Django bootstrap — gate kill-switches via
``teatree_settings.teatree_bool_setting`` and the integer budgets in ``hook_router``
via ``teatree_settings.teatree_int_setting`` (config-unify PR3/PR4 flipped both onto
the DB store, TOML-fallback). The TOML->DB import used to walk only
``OVERLAY_OVERRIDABLE_SETTINGS``, so these keys were dropped on import and would
reset to their in-code defaults the moment the cold reader is flipped onto the DB
store. These tests MECHANICALLY enumerate the live cold-read sites and assert each
key has a registered home, so a new hook gate flag or budget added without a
``COLD_HOOK_SETTINGS`` entry turns the suite red — the guard against the lossy
cutover recurring.
"""

import dataclasses
import re
from pathlib import Path

import pytest

from teatree.config import (
    COLD_HOOK_SETTINGS,
    OVERLAY_OVERRIDABLE_SETTINGS,
    SETTING_HOMES,
    TOML_OVERLAY_OVERRIDABLE_SETTINGS,
    ColdHookSetting,
    SettingHome,
    UserSettings,
)
from teatree.config.setting_parsers import _parse_strict_bool, _parse_strict_int

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK_SCRIPTS = _REPO_ROOT / "hooks" / "scripts"

# ``teatree_bool_setting("x")`` / ``_teatree_bool_setting("x")`` — the shared cold
# ``[teatree] <flag>`` boolean adapter. A call captures the flag's exact key.
_BOOL_FLAG_CALL = re.compile(r"(?:_)?teatree_bool_setting\(\s*[\"']([a-z0-9_]+)[\"']")

# Same call WITH its ``default=`` so the registered default can be pinned to the
# hook's own fallback — a drift between them would seed the wrong DB default.
_BOOL_FLAG_DEFAULT = re.compile(
    r"(?:_)?teatree_bool_setting\(\s*[\"']([a-z0-9_]+)[\"']\s*,\s*default=(True|False)\s*\)",
)

# The ``[teatree] <key>`` integer budgets ``hook_router`` reads cold through the
# shared ``teatree_int_setting`` / ``section_int_setting`` adapter (config-unify PR4,
# replacing the old inline ``teatree.get(...)`` ``tomllib`` reads). ``_KEY`` regexes
# capture the budget key from either call shape; ``_DEFAULT`` regexes also capture the
# ``default=`` constant so the registered default can be pinned to the hook's own
# ``_DEFAULT_*`` fallback (a drift would seed the wrong DB default).
_INT_BUDGET_KEY = re.compile(r"(?:_)?teatree_int_setting\(\s*[\"']([a-z0-9_]+)[\"']")
_SECTION_INT_BUDGET_KEY = re.compile(r"(?:_)?section_int_setting\(\s*[\"']teatree[\"']\s*,\s*[\"']([a-z0-9_]+)[\"']")
_INT_BUDGET_DEFAULT = re.compile(
    r"(?:_)?teatree_int_setting\(\s*[\"']([a-z0-9_]+)[\"']\s*,\s*default=(_[A-Z][A-Z0-9_]*)",
)
_SECTION_INT_BUDGET_DEFAULT = re.compile(
    r"(?:_)?section_int_setting\(\s*[\"']teatree[\"']\s*,\s*[\"']([a-z0-9_]+)[\"']\s*,\s*default=(_[A-Z][A-Z0-9_]*)",
)
# Module-level ``_UPPER_CONST = <int>`` assignments (the default constants).
_MODULE_INT_CONST = re.compile(r"^(_[A-Z][A-Z0-9_]*)\s*=\s*(-?\d+)\b", re.MULTILINE)


def _enumerate_cold_int_budgets() -> set[str]:
    """Every ``[teatree]`` integer budget read cold via the int adapter, across the hook leaves."""
    keys: set[str] = set()
    for script in _HOOK_SCRIPTS.glob("*.py"):
        text = script.read_text(encoding="utf-8")
        keys.update(_INT_BUDGET_KEY.findall(text))
        keys.update(_SECTION_INT_BUDGET_KEY.findall(text))
    return keys


def _registered_int_keys() -> set[str]:
    """The COLD_HOOK_SETTINGS keys whose default is an int (the budgets, not the bool gates)."""
    return {k for k, s in COLD_HOOK_SETTINGS.items() if isinstance(s.default, int) and not isinstance(s.default, bool)}


def _toml_home_user_settings_fields() -> set[str]:
    """The TOML-home ``UserSettings`` fields (e.g. ``speak``, ``mr_reminder``).

    Cold readers consult these too, but they have a recognised TOML home under the
    #1775 partition, so they are intentionally NOT in ``COLD_HOOK_SETTINGS``.
    """
    return {key for key, home in SETTING_HOMES.items() if home is SettingHome.TOML}


def _recognised_homes() -> set[str]:
    """Every registry a cold-read ``[teatree]`` key may legitimately live in."""
    return (
        set(COLD_HOOK_SETTINGS)
        | set(OVERLAY_OVERRIDABLE_SETTINGS)
        | set(TOML_OVERLAY_OVERRIDABLE_SETTINGS)
        | _toml_home_user_settings_fields()
    )


def _enumerate_cold_bool_flags() -> set[str]:
    """Every ``[teatree]`` gate flag read cold via the bool adapter, across the hook leaves."""
    keys: set[str] = set()
    for script in _HOOK_SCRIPTS.glob("*.py"):
        keys.update(_BOOL_FLAG_CALL.findall(script.read_text(encoding="utf-8")))
    return keys


def test_enumeration_is_not_vacuous() -> None:
    # Guard the guard: a broken regex / moved hook dir must not make the coverage
    # tests pass against an empty enumeration.
    flags = _enumerate_cold_bool_flags()
    assert _HOOK_SCRIPTS.is_dir()
    assert "deny_circuit_breaker_enabled" in flags
    assert len(flags) >= 10


def test_every_cold_bool_flag_has_a_registered_home() -> None:
    # The recurrence guard: a new ``teatree_bool_setting("new_gate_enabled")`` added
    # without a COLD_HOOK_SETTINGS entry (and not a recognised TOML-home field) is
    # an unregistered cold-hook key that the import would silently drop.
    unregistered = sorted(_enumerate_cold_bool_flags() - _recognised_homes())
    assert unregistered == [], f"cold-hook gate flags with no registered home: {unregistered}"


def test_cold_bool_flag_defaults_match_the_hook_default() -> None:
    # Pin each registered default to the hook's own ``default=`` so the DB-seeded
    # default can never drift from what the cold reader falls back to.
    declared: dict[str, bool] = {}
    for script in _HOOK_SCRIPTS.glob("*.py"):
        for key, value in _BOOL_FLAG_DEFAULT.findall(script.read_text(encoding="utf-8")):
            declared[key] = value == "True"
    registered = {key: want for key, want in declared.items() if key in COLD_HOOK_SETTINGS}
    drifted = sorted(key for key, want in registered.items() if COLD_HOOK_SETTINGS[key].default is not want)
    assert drifted == [], f"registered cold-hook default disagrees with the hook: {drifted}"
    assert len(registered) >= 10


def test_int_budget_cold_reads_equal_the_registered_int_keys() -> None:
    # The mechanized int-side no-silent-drop guard (teatree task #25, #2794 review
    # MED). The live ``teatree_int_setting`` / ``section_int_setting`` cold-read sites
    # must EXACTLY equal the int keys registered in COLD_HOOK_SETTINGS: a new budget
    # read added without a registration (the import would drop it) OR a registered int
    # key with no live reader turns this red. No hand-frozen allowlist.
    budgets = _enumerate_cold_int_budgets()

    # Anti-vacuity: the three known budgets must surface, so a broken regex / moved
    # reader cannot make the guard pass against an empty enumeration.
    assert {
        "deny_circuit_breaker_threshold",
        "orchestrator_turn_budget",
        "orchestrator_turn_wall_clock_seconds",
    } <= budgets

    assert budgets == _registered_int_keys()


def test_int_budget_defaults_match_the_registered_default() -> None:
    # Pin each budget's ``default=`` constant to the registered COLD_HOOK_SETTINGS
    # default so the DB-seeded default can never drift from the hook's own fallback.
    const_values: dict[str, int] = {}
    declared: dict[str, str] = {}
    for script in _HOOK_SCRIPTS.glob("*.py"):
        text = script.read_text(encoding="utf-8")
        const_values.update({name: int(value) for name, value in _MODULE_INT_CONST.findall(text)})
        declared.update(_INT_BUDGET_DEFAULT.findall(text))
        declared.update(_SECTION_INT_BUDGET_DEFAULT.findall(text))

    assert set(declared) == _registered_int_keys(), "every registered int budget must expose a resolvable default="
    for key, const in declared.items():
        hook_default = const_values[const]
        registered = COLD_HOOK_SETTINGS[key].default
        assert registered == hook_default, (
            f"registered default for {key!r} ({registered}) drifted from the hook default {hook_default}"
        )


def test_cold_hook_settings_disjoint_from_overridable_registry() -> None:
    # A key cannot have two homes: the cold-hook keys are exactly the ones the
    # overridable (DB-home) registry does NOT carry.
    overlap = set(COLD_HOOK_SETTINGS) & set(OVERLAY_OVERRIDABLE_SETTINGS)
    assert overlap == set(), f"cold-hook keys must not also be overridable settings: {sorted(overlap)}"


def test_cold_hook_keys_are_not_user_settings_fields() -> None:
    # These are hook-leaf gate flags / budgets with no dataclass field — distinct
    # from the TOML-home ``UserSettings`` fields the cold readers also consult.
    fields = {f.name for f in dataclasses.fields(UserSettings)}
    overlap = sorted(set(COLD_HOOK_SETTINGS) & fields)
    assert overlap == [], f"cold-hook keys must not be UserSettings fields: {overlap}"


def test_every_cold_hook_setting_is_global_scope() -> None:
    non_global = sorted(k for k, s in COLD_HOOK_SETTINGS.items() if s.scope != "")
    assert non_global == [], f"cold-hook settings must resolve from the GLOBAL scope: {non_global}"


def test_each_default_round_trips_through_its_parser() -> None:
    # The registered default must be a valid, type-correct value for its parser —
    # the same parser the import coerces a stored value through.
    for key, setting in COLD_HOOK_SETTINGS.items():
        assert setting.parse(setting.default) == setting.default, key


def test_parsers_match_the_declared_default_type() -> None:
    for key, setting in COLD_HOOK_SETTINGS.items():
        if isinstance(setting.default, bool):
            assert setting.parse is _parse_strict_bool, f"{key} bool default needs the strict bool parser"
        else:
            assert isinstance(setting.default, int)
            assert setting.parse is _parse_strict_int, f"{key} int default needs the strict int parser"


def test_dataclass_is_frozen() -> None:
    setting = COLD_HOOK_SETTINGS["deny_circuit_breaker_enabled"]
    assert isinstance(setting, ColdHookSetting)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setting.default = False  # type: ignore[misc]
