# test-path: cross-cutting
"""Every ``UserSettings`` field has exactly one home — DB or TOML (#1775).

The hard partition: a setting that CAN live in the DB MUST be DB-home. As of
eliminate-~/.teatree.toml the carve-out is EMPTY — every field is DB-home (the
final two, the nested ``speak`` / ``mr_reminder`` tables, moved to JSON-dict
``ConfigSetting`` rows). One field is DERIVED — the resolver computes it, so it
has no home and is excluded from the partition.

The fitness functions below make the partition machine-checked: they go RED the
moment a new ``UserSettings`` field is added without classifying it, or a field
lands in both homes.
"""

import dataclasses

from teatree.config import DERIVED_FIELDS, SETTING_HOMES, SettingHome, UserSettings

# eliminate-~/.teatree.toml: the carve-out is EMPTY — every UserSettings field is DB-home.
_TOML_CARVE_OUT: frozenset[str] = frozenset()


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


def test_toml_carve_out_is_empty() -> None:
    # eliminate-~/.teatree.toml is COMPLETE: the TOML-home carve-out is empty — every
    # ``UserSettings`` field is DB-home. The last two fields, the nested structured
    # tables ``speak`` and ``mr_reminder``, moved to JSON-dict ``ConfigSetting`` rows
    # rebuilt bespoke by the resolver. (Earlier moves: ``check_updates``,
    # ``worktrees_dir`` / ``timezone``, ``orchestrator_bash_gate_enabled`` /
    # ``privacy``, ``handover_mirror_path``, ``statusline_chain``, ``autoload``.)
    toml_home = {k for k, home in SETTING_HOMES.items() if home is SettingHome.TOML}
    assert toml_home == frozenset()
    moved_to_db = (
        "workspace_dir",
        "check_updates",
        "worktrees_dir",
        "timezone",
        "handover_mirror_path",
        "statusline_chain",
        "autoload",
        "speak",
        "mr_reminder",
    )
    for moved in moved_to_db:
        assert moved not in toml_home
        assert SETTING_HOMES[moved] is SettingHome.DB


def test_falsely_bootstrap_fields_are_db_home() -> None:
    # ``worktrees_dir`` / ``timezone`` were tagged "needed to open the DB", but
    # Django ``settings.py`` hardcodes ``TIME_ZONE = "UTC"`` and configures
    # ``DATABASES`` without reading either — so both are DB-home, not bootstrap.
    assert SETTING_HOMES["worktrees_dir"] is SettingHome.DB
    assert SETTING_HOMES["timezone"] is SettingHome.DB


def test_per_overlay_toml_fields_collapsed_to_db_home() -> None:
    # eliminate-~/.teatree.toml: the two former per-overlay-TOML-overridable fields
    # are DB-home — per-overlay override now lives in a ``ConfigSetting`` overlay
    # row, not ``[overlays.<name>]``. The gate reader is DB-first (cold_reader).
    assert SETTING_HOMES["orchestrator_bash_gate_enabled"] is SettingHome.DB
    assert SETTING_HOMES["privacy"] is SettingHome.DB


def test_handover_mirror_path_is_db_home() -> None:
    # eliminate-~/.teatree.toml: the SessionStart bootstrap reader
    # (``hook_router``) now reads ``handover_mirror_path`` via the Django-free
    # ``cold_reader``, which fails open to ``_default_handover_mirror_path()`` —
    # the exact path ``write_mirror`` uses when unset — so the "read when the DB
    # is unreachable" carve-out is satisfied without TOML.
    assert SETTING_HOMES["handover_mirror_path"] is SettingHome.DB


def test_statusline_chain_is_db_home() -> None:
    # eliminate-~/.teatree.toml: the bash statusline hook reads ``statusline_chain``
    # from the canonical sqlite via the ``sqlite3`` CLI + ``json_each`` (no
    # importable teatree python, no TOML parse), so it is DB-home.
    assert SETTING_HOMES["statusline_chain"] is SettingHome.DB


def test_autoload_is_db_home() -> None:
    # eliminate-~/.teatree.toml: the #256 engagement flag ``autoload`` is DB-home.
    # Its cold pre-Django readers — ``teatree_settings._cold_db_bool`` (Python) and
    # the bash ``statusline.sh._autoload_db_value`` (sqlite3 CLI) — read the
    # canonical sqlite directly, so it needs no TOML; a ``[teatree] autoload`` value
    # is ignored on read.
    assert SETTING_HOMES["autoload"] is SettingHome.DB


def test_speak_is_db_home() -> None:
    # eliminate-~/.teatree.toml: ``speak`` is DB-home — stored as a JSON-dict
    # ``ConfigSetting`` row (``parse_speak_setting``), rebuilt bespoke by the resolver.
    # The cold Stop-hook reader (``hook_router._speak_settings``) reads it via
    # ``cold_reader.read_setting`` (a dict), so it needs no tomllib.
    assert SETTING_HOMES["speak"] is SettingHome.DB


def test_mr_reminder_is_db_home() -> None:
    # eliminate-~/.teatree.toml: ``mr_reminder`` is DB-home — stored as a JSON-dict
    # ``ConfigSetting`` row (``parse_mr_reminder_setting``), rebuilt bespoke by the
    # resolver (overlay-then-global). The last nested structured table to move.
    assert SETTING_HOMES["mr_reminder"] is SettingHome.DB


def test_check_updates_is_db_home() -> None:
    # eliminate-~/.teatree.toml: ``check_updates``'s sole reader ``check_for_updates``
    # runs on PRE-DJANGO paths (the CLI root callback, the plain-Typer ``t3 config
    # check-update``) — but it now reads the ``ConfigSetting`` store via the
    # Django-free ``cold_reader``, so a stored ``check_updates=false`` IS honoured
    # with no Django bootstrap. The "DB read fails safe to the default" concern that
    # kept it TOML-home is closed by the cold reader. The behavioural guard is
    # ``test_check_for_updates`` § ``test_disabled_check_honoured_pre_django_via_db``.
    assert SETTING_HOMES["check_updates"] is SettingHome.DB


def test_derived_fields_are_exactly_the_one_computed_value() -> None:
    assert frozenset({"notify_on_behalf"}) == DERIVED_FIELDS


def test_db_home_covers_every_non_carve_out_non_derived_field() -> None:
    # The A1 invariant: every field that is neither carve-out nor derived is DB-home.
    db_home = {k for k, home in SETTING_HOMES.items() if home is SettingHome.DB}
    expected = _all_field_names() - _TOML_CARVE_OUT - DERIVED_FIELDS
    assert db_home == expected
