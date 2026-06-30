"""The DB/TOML hard partition for every ``UserSettings`` field (#1775).

Every non-derived ``UserSettings`` field has EXACTLY ONE home.

:attr:`SettingHome.DB` — the field's sole authoritative tier is the
``ConfigSetting`` store (global + per-overlay rows) plus the ``T3_*`` env layer.
The ``[teatree]`` / ``[overlays.<name>]`` TOML tables are NOT read for it: a TOML
value for a DB-home key is ignored on read (its home is the DB), so an install
moving to the partition migrates such keys into the store with ``t3 <overlay>
config_setting import``.

:attr:`SettingHome.TOML` — the field's sole authoritative tier is the
``[teatree]`` / ``[overlays.<name>]`` TOML tables plus the ``T3_*`` env layer. A
``ConfigSetting`` row for a TOML-home key is ignored on read; ``config_setting
set`` refuses to write one.

The TOML-home carve-out is now EMPTY (eliminate-~/.teatree.toml): EVERY
``UserSettings`` field is DB-home. The last two fields — the nested structured
tables ``speak`` and ``mr_reminder`` — moved to the DB store as JSON-dict
``ConfigSetting`` rows (``parse_speak_setting`` / ``parse_mr_reminder_setting``),
rebuilt bespoke by the resolver (``resolution._BESPOKE_STRUCTURED_FIELDS``). The
cold Stop-hook ``speak`` reader now reads the canonical sqlite via
``cold_reader.read_setting`` (a dict), so it needs no tomllib. A ``[teatree]`` /
``[overlays.<name>]`` value for ANY ``UserSettings`` field is ignored on read (its
home is the DB) and the resolver warns on it. ``workspace_dir`` and ``worktrees_dir``
resolve Django-side off the store (Django ``settings.py`` hardcodes ``TIME_ZONE`` and
configures ``DATABASES`` without reading either, so neither was ever a DB-open
bootstrap dep); ``handover_mirror_path`` / ``statusline_chain`` / ``autoload`` resolve
from the store on their pre-Django paths via ``cold_reader`` / the ``sqlite3`` CLI.

:data:`DERIVED_FIELDS` is the one value the resolver COMPUTES rather than
reads (``notify_on_behalf`` derived by the autonomy collapse); it has
no home and is excluded from the partition.

The fitness functions in ``tests/config/test_settings_home_partition.py`` keep
this exhaustive and disjoint: every ``UserSettings`` field is in exactly one of
:data:`SETTING_HOMES` / :data:`DERIVED_FIELDS`, and the two homes never overlap.
"""

from enum import StrEnum


class SettingHome(StrEnum):
    """The single authoritative tier of a ``UserSettings`` field."""

    DB = "db"
    TOML = "toml"


# The one value the resolver computes rather than reads — no home, excluded
# from the partition. ``notify_on_behalf`` is ORed in by the autonomy collapse.
DERIVED_FIELDS: frozenset[str] = frozenset({"notify_on_behalf"})

# The TOML-home carve-out is now EMPTY — eliminate-~/.teatree.toml moved EVERY
# ``UserSettings`` field to the DB store. The final two were the nested structured
# tables ``speak`` and ``mr_reminder``: each is stored as a JSON-dict
# ``ConfigSetting`` row (``parse_speak_setting`` / ``parse_mr_reminder_setting``) and
# rebuilt bespoke by the resolver (``resolution._BESPOKE_STRUCTURED_FIELDS``) since a
# dict cannot flat-replace the dataclass field. The cold Stop-hook ``speak`` reader
# (``hook_router._speak_settings``) now reads the canonical sqlite via
# ``cold_reader.read_setting`` (a dict), so it needs no tomllib.
#
# Earlier moves (still DB-home): ``check_updates``, ``worktrees_dir`` / ``timezone``,
# the two former per-overlay-TOML-overridable fields ``orchestrator_bash_gate_enabled``
# / ``privacy`` (per-overlay override now lives in a ``ConfigSetting`` overlay-scope
# row), ``handover_mirror_path``, and ``statusline_chain`` / ``autoload`` (read from
# the canonical sqlite on their pre-Django paths via ``cold_reader`` / the ``sqlite3``
# CLI). ``workspace_dir`` / ``worktrees_dir`` are read only after Django is up; the
# worktree root regroups under ``~/workspace/t3-workspaces/<overlay>/``
# (``config.worktree_root()``), distinct from the CLONE root ``config.clone_root()``.
#
# An empty carve-out is the end state of the partition: a NEW ``UserSettings`` field
# is DB-home by default (``_build_setting_homes``); adding one here again would need a
# genuine pre-Django/bootstrap justification that ``cold_reader`` cannot satisfy.
_TOML_HOME: frozenset[str] = frozenset()

# Every DB-home field: the canonical list, built once below from the
# ``UserSettings`` dataclass minus the carve-out and the derived fields, so the
# registry can never drift out of sync with the dataclass.


def _build_setting_homes() -> dict[str, SettingHome]:
    """Build the exhaustive home registry from the live ``UserSettings`` fields.

    Computed from ``dataclasses.fields`` so a new field is DB-home by default
    (the A1 rule: a field that CAN live in the DB MUST be DB-home). The carve-out
    is the only TOML-home set; the two derived fields are excluded entirely. The
    import is deferred to avoid a settings -> homes -> settings cycle at module
    load.
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
