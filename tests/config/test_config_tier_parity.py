# test-path: cross-cutting
"""The DB-home parser registry covers exactly the DB-home set (#1775 partition).

Under the hard partition, ``OVERLAY_OVERRIDABLE_SETTINGS`` is the DB-home parser
registry: it must have an entry for EVERY DB-home ``UserSettings`` field (so the
resolver can coerce its stored value) and NO entry for a TOML-home or DERIVED
field (a DB-home key is the only thing the DB tier supplies). The fitness
functions below go RED the moment a DB-home field gains no parser, or a
TOML-home/derived field is wrongly registered.

The companion ``TOML_OVERLAY_OVERRIDABLE_SETTINGS`` is the per-overlay TOML
parser registry for TOML-home keys; its keys must all be TOML-home.
"""

import dataclasses

from teatree.config import (
    DERIVED_FIELDS,
    OVERLAY_OVERRIDABLE_SETTINGS,
    SETTING_HOMES,
    TOML_OVERLAY_OVERRIDABLE_SETTINGS,
    SettingHome,
    UserSettings,
)


def _db_home_fields() -> set[str]:
    return {k for k, home in SETTING_HOMES.items() if home is SettingHome.DB}


def _toml_home_fields() -> set[str]:
    return {k for k, home in SETTING_HOMES.items() if home is SettingHome.TOML}


def test_db_home_registry_covers_every_db_home_field() -> None:
    missing = sorted(_db_home_fields() - set(OVERLAY_OVERRIDABLE_SETTINGS))
    assert missing == [], f"DB-home fields with no parser in OVERLAY_OVERRIDABLE_SETTINGS: {missing}"


def test_db_home_registry_has_no_toml_home_or_derived_key() -> None:
    extra = sorted(set(OVERLAY_OVERRIDABLE_SETTINGS) - _db_home_fields())
    assert extra == [], f"OVERLAY_OVERRIDABLE_SETTINGS keys that are not DB-home: {extra}"


def test_db_home_registry_keys_are_all_user_settings_fields() -> None:
    field_names = {f.name for f in dataclasses.fields(UserSettings)}
    bogus = sorted(set(OVERLAY_OVERRIDABLE_SETTINGS) - field_names)
    assert bogus == [], f"registry keys that are not UserSettings fields: {bogus}"


def test_no_derived_field_is_db_overridable() -> None:
    overlap = sorted(DERIVED_FIELDS & set(OVERLAY_OVERRIDABLE_SETTINGS))
    assert overlap == [], f"derived fields must never be DB-overridable: {overlap}"


def test_toml_overlay_registry_keys_are_all_toml_home() -> None:
    not_toml = sorted(set(TOML_OVERLAY_OVERRIDABLE_SETTINGS) - _toml_home_fields())
    assert not_toml == [], f"TOML_OVERLAY_OVERRIDABLE_SETTINGS keys that are not TOML-home: {not_toml}"


def test_db_and_toml_overlay_registries_are_disjoint() -> None:
    overlap = sorted(set(OVERLAY_OVERRIDABLE_SETTINGS) & set(TOML_OVERLAY_OVERRIDABLE_SETTINGS))
    assert overlap == [], f"a key cannot be in both override registries: {overlap}"
