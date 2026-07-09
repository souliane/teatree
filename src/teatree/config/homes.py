"""The DB home of every ``UserSettings`` field.

Every non-derived ``UserSettings`` field is DB-home: its sole authoritative tier
is the ``ConfigSetting`` store (global + per-overlay rows) plus the ``T3_*`` env
layer. There is no config file — the last file tier was removed, so the resolver
reads each field from the store (Django-side via
``ConfigSetting.objects.get_effective``, pre-Django via ``cold_reader``). The two
structured fields ``speak`` / ``mr_reminder`` are stored as JSON-dict rows and
rebuilt bespoke by the resolver (``resolution._BESPOKE_STRUCTURED_FIELDS``);
``workspace_dir`` / ``worktrees_dir`` resolve Django-side off the store;
``handover_mirror_path`` / ``statusline_chain`` / ``autoload`` resolve from the
store on their pre-Django paths via ``cold_reader`` / the ``sqlite3`` CLI.

:data:`DERIVED_FIELDS` is the one value the resolver COMPUTES rather than
reads (``notify_on_behalf`` derived by the autonomy collapse); it has
no home and is excluded from the partition.

The fitness functions in ``tests/config/test_settings_home_partition.py`` keep
this exhaustive: every ``UserSettings`` field is in exactly one of
:data:`SETTING_HOMES` / :data:`DERIVED_FIELDS`.
"""

from enum import StrEnum


class SettingHome(StrEnum):
    """The single authoritative tier of a ``UserSettings`` field.

    Every field is :attr:`DB` now (the config file was removed). :attr:`TOML`
    remains as the empty legacy carve-out the fitness functions assert is empty,
    so a future re-introduction of a file tier would be a deliberate, tested move.
    """

    DB = "db"
    TOML = "toml"


# The irreducible bootstrap set: settings that must be readable BEFORE Django —
# and therefore the DB — is available, so they can never move into the
# ``ConfigSetting`` store. ``DATABASE_URL`` / ``data_dir`` / ``DJANGO_SETTINGS_MODULE``
# are the ENV keys the settings module itself needs to even OPEN the DB. This typed
# allowlist is the single machine-checked home for that boundary: the
# disjoint-registries invariant ``BOOTSTRAP_ENV_ONLY_SETTINGS ∩
# OVERLAY_OVERRIDABLE_SETTINGS == ∅`` (a fitness function in the tests) makes it
# impossible to make a bootstrap key DB-overridable without turning a test red, and
# ``config_setting set`` already refuses every key here (none is in the overridable
# registry) so an admin can never stash a DB row for a bootstrap-only setting.
BOOTSTRAP_ENV_ONLY_SETTINGS: frozenset[str] = frozenset(
    {
        "DATABASE_URL",
        "data_dir",
        "DJANGO_SETTINGS_MODULE",
    }
)


# The one value the resolver computes rather than reads — no home, excluded
# from the partition. ``notify_on_behalf`` is ORed in by the autonomy collapse.
DERIVED_FIELDS: frozenset[str] = frozenset({"notify_on_behalf"})

# The TOML-home carve-out is EMPTY — every ``UserSettings`` field is DB-home. Kept
# as a named empty set so the fitness functions can assert emptiness and a future
# re-introduction of a file tier is a deliberate, reviewed change.
_TOML_HOME: frozenset[str] = frozenset()

# Every ``UserSettings`` field is DB-home: the canonical list, built once below from
# the ``UserSettings`` dataclass minus the derived fields, so the registry can never
# drift out of sync with the dataclass.


def _build_setting_homes() -> dict[str, SettingHome]:
    """Build the exhaustive home registry from the live ``UserSettings`` fields.

    Computed from ``dataclasses.fields`` so a new field is DB-home by default (the
    carve-out is empty). The two derived fields are excluded entirely. The import is
    deferred to avoid a settings -> homes -> settings cycle at module load.
    """
    import dataclasses  # noqa: PLC0415

    from teatree.config.settings import UserSettings  # noqa: PLC0415

    homes: dict[str, SettingHome] = {}
    for field in dataclasses.fields(UserSettings):
        if field.name in DERIVED_FIELDS:
            continue
        homes[field.name] = SettingHome.TOML if field.name in _TOML_HOME else SettingHome.DB
    return homes


SETTING_HOMES: dict[str, SettingHome] = _build_setting_homes()
