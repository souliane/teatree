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

The TOML-home set is the irreducible carve-out: settings a NON-DJANGO or
PRE-DJANGO reader needs (so the DB is unreachable — ``orchestrator_bash_gate_enabled``,
``speak``, ``handover_mirror_path``, ``check_updates``, ``autoload`` (the cold
SessionStart / UserPromptSubmit hooks read it pre-Django to decide engagement),
and ``statusline_chain``, which the bash statusline hook reads straight from
``~/.teatree.toml`` and can never reach the DB), path/infra bootstrap that the settings module itself needs
(``workspace_dir``, ``worktrees_dir``, ``timezone``,
``privacy``), and nested structured tables that have no flat scalar shape for a
``ConfigSetting`` row (``mr_reminder``). Every other field is DB-home — it resolves
from the ``ConfigSetting`` store + env, never from a ``[teatree]`` /
``[overlays.<name>]`` TOML value (which is ignored on read and the resolver warns
on).

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

# The irreducible TOML-home carve-out (exactly these eleven):
# - non-Django / pre-Django readers (read via tomllib or a bash grep, no DB):
#   ``orchestrator_bash_gate_enabled``, ``speak`` (the Stop hook re-reads the
#   ``[teatree.speak]`` sub-table with tomllib — it cannot reach the Django DB),
#   ``handover_mirror_path`` (the SessionStart bootstrap path read precisely when
#   the DB is unreachable), ``check_updates``, ``autoload`` (the cold SessionStart
#   / UserPromptSubmit hooks read ``[teatree] autoload`` with tomllib to decide
#   default-off engagement, before any Django bootstrap — #256), and
#   ``statusline_chain`` (the bash statusline hook reads ``[teatree]
#   statusline_chain`` straight from ``~/.teatree.toml`` — it has no path to the
#   Django DB, so a DB row for it would be silently unread)
# - path / infra bootstrap the settings module needs to even open the DB:
#   ``workspace_dir``, ``worktrees_dir``, ``timezone``, ``privacy``
# - nested structured table with no flat ConfigSetting shape: ``mr_reminder``
_TOML_HOME: frozenset[str] = frozenset(
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
