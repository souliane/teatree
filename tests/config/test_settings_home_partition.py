# test-path: cross-cutting
"""Every ``UserSettings`` field has exactly one home — DB or TOML (#1775).

The hard partition: a setting that CAN live in the DB MUST be DB-home; only the
irreducible carve-out (pre-Django readers, path/infra bootstrap, nested
structured tables, dead fields) stays TOML-home. One field is DERIVED — the
resolver computes it, so it has no home and is excluded from the partition.

The fitness functions below make the partition machine-checked: they go RED the
moment a new ``UserSettings`` field is added without classifying it, or a field
lands in both homes.
"""

import dataclasses

from teatree.config import DERIVED_FIELDS, SETTING_HOMES, SettingHome, UserSettings

_TOML_CARVE_OUT = frozenset(
    {
        "orchestrator_bash_gate_enabled",
        "speak",
        "mr_reminder",
        "handover_mirror_path",
        "check_updates",
        "autoload",
        "statusline_chain",
        "workspace_dir",
        "worktrees_dir",
        "timezone",
        "privacy",
    }
)


def _all_field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(UserSettings)}


def test_every_user_settings_field_has_exactly_one_classification() -> None:
    # Coverage over every field: each is in SETTING_HOMES xor DERIVED_FIELDS.
    fields = _all_field_names()
    classified = set(SETTING_HOMES) | DERIVED_FIELDS
    missing = sorted(fields - classified)
    extra = sorted(classified - fields)
    assert missing == [], f"UserSettings fields with no home/derived classification: {missing}"
    assert extra == [], f"classified names that are not UserSettings fields: {extra}"


def test_no_field_is_both_homed_and_derived() -> None:
    overlap = set(SETTING_HOMES) & DERIVED_FIELDS
    assert overlap == set(), f"a field cannot be both homed and derived: {overlap}"


def test_db_home_and_toml_home_are_disjoint() -> None:
    # The ticket's required assertion: a field cannot have two homes.
    db_home = {k for k, home in SETTING_HOMES.items() if home is SettingHome.DB}
    toml_home = {k for k, home in SETTING_HOMES.items() if home is SettingHome.TOML}
    assert db_home & toml_home == set(), "DB-home and TOML-home must be disjoint"
    # And the two homes partition SETTING_HOMES exhaustively.
    assert db_home | toml_home == set(SETTING_HOMES)


def test_toml_carve_out_is_exactly_the_eleven_fields() -> None:
    # The irreducible carve-out — non-Django / pre-Django readers, infra
    # bootstrap, nested structured tables — is exactly these eleven and no more.
    toml_home = {k for k, home in SETTING_HOMES.items() if home is SettingHome.TOML}
    assert toml_home == _TOML_CARVE_OUT


def test_autoload_is_toml_home_not_db() -> None:
    # #256: the cold SessionStart / UserPromptSubmit hooks read ``[teatree]
    # autoload`` pre-Django, so it must be TOML-home (ignored from the DB store
    # exactly like ``check_updates``), never DB-home.
    assert SETTING_HOMES["autoload"] is SettingHome.TOML


def test_derived_fields_are_exactly_the_one_computed_value() -> None:
    assert frozenset({"notify_on_behalf"}) == DERIVED_FIELDS


def test_db_home_covers_every_non_carve_out_non_derived_field() -> None:
    # The A1 invariant: every field that is neither carve-out nor derived is DB-home.
    db_home = {k for k, home in SETTING_HOMES.items() if home is SettingHome.DB}
    expected = _all_field_names() - _TOML_CARVE_OUT - DERIVED_FIELDS
    assert db_home == expected
