# test-path: cross-cutting
"""Every overridable setting must be a ``UserSettings`` field AND wired in ``load_config``.

A setting registered in ``OVERLAY_OVERRIDABLE_SETTINGS`` advertises that its
global ``[teatree]`` value (and per-overlay/DB override) is honoured. That promise
is only real if two things hold:

1.  the value has a home — a ``UserSettings`` field;
2.  ``load_config`` actually reads it off the ``[teatree]`` table into that field.

When (1) holds but (2) does not, the global ``~/.teatree.toml`` value is silently
dead: the overlay/DB override machinery may still touch the key, but a user who
sets it globally gets the default with no error. ``provision_step_timeout_seconds``
was exactly this — registered as overridable, a ``UserSettings`` field, documented
in ``configuration.md``, yet never wired into ``load_config``.

The parity fitness functions below catch the whole dead-config class: they go RED
the moment an overridable key gains no ``UserSettings`` field or is left unwired in
``load_config``.
"""

import dataclasses
import inspect
import re
from pathlib import Path

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, load_config, loader
from teatree.config.settings import BOOTSTRAP_FILE_ONLY_SETTINGS, UserSettings

from ._shared import _write_toml

# Overridable keys whose value reaches ``UserSettings`` through a path other than a
# ``[teatree]``-table read in ``load_config``. ``T3_*`` env vars are read by
# ``get_effective_settings`` (not ``load_config``), so a setting that exists ONLY as
# an env override would have no ``[teatree]`` read — none currently does, so this is
# empty. It exists as the explicit, reviewed home for that exception rather than a
# silent gap, mirroring ``BOOTSTRAP_FILE_ONLY_SETTINGS``.
ENV_ONLY_OVERRIDABLE_SETTINGS: frozenset[str] = frozenset()


def _load_config_wiring_source() -> str:
    """Source of ``load_config`` plus every resolver helper it calls.

    Several overridable keys are read inside a helper (``_resolve_teams_int``,
    ``_resolve_enum_setting``, ``resolve_speak``…) rather than via a direct
    ``teatree.get(...)`` in ``load_config``'s body. The wiring check must see the
    helper bodies too, or it would false-RED on those keys.
    """
    source = inspect.getsource(loader.load_config)
    for helper_name in sorted(set(re.findall(r"\b(_resolve_[a-z_]+|resolve_[a-z_]+)\s*\(", source))):
        helper = getattr(loader, helper_name, None)
        if helper is None:
            continue
        try:
            source += "\n" + inspect.getsource(helper)
        except (OSError, TypeError):
            continue
    return source


def _overridable_keys_under_parity() -> set[str]:
    return set(OVERLAY_OVERRIDABLE_SETTINGS) - BOOTSTRAP_FILE_ONLY_SETTINGS - ENV_ONLY_OVERRIDABLE_SETTINGS


def test_every_overridable_setting_is_a_user_settings_field() -> None:
    field_names = {f.name for f in dataclasses.fields(UserSettings)}
    missing = sorted(_overridable_keys_under_parity() - field_names)
    assert missing == [], f"overridable keys with no UserSettings field: {missing}"


def test_every_overridable_setting_is_wired_in_load_config() -> None:
    source = _load_config_wiring_source()
    unwired = sorted(k for k in _overridable_keys_under_parity() if k not in source)
    assert unwired == [], f"overridable keys never read in load_config (dead global config): {unwired}"


def test_parity_exception_registries_are_disjoint_from_each_other() -> None:
    overlap = BOOTSTRAP_FILE_ONLY_SETTINGS & ENV_ONLY_OVERRIDABLE_SETTINGS
    assert overlap == set(), f"a key cannot be both bootstrap-file-only and env-only: {overlap}"


def test_parity_exceptions_are_actually_overridable_keys() -> None:
    # An env-only exception only makes sense for a key that IS overridable; a stale
    # name in the exception set would silently shrink the parity check's coverage.
    bogus = sorted(ENV_ONLY_OVERRIDABLE_SETTINGS - set(OVERLAY_OVERRIDABLE_SETTINGS))
    assert bogus == [], f"env-only exceptions that are not overridable keys: {bogus}"


def test_global_provision_step_timeout_seconds_flows_through_load_config(tmp_path: Path) -> None:
    # The concrete dead-config case the parity check generalises: a global
    # ``[teatree]`` value reaches ``UserSettings`` (was silently dropped before wiring).
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nprovision_step_timeout_seconds = 42\n")
    assert load_config(config_path).user.provision_step_timeout_seconds == 42


def test_provision_step_timeout_seconds_defaults_when_unset(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.provision_step_timeout_seconds == 1800
