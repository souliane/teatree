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
``speak``, ``handover_mirror_path``, ``check_updates``, and ``statusline_chain``,
which the bash statusline hook reads straight from ``~/.teatree.toml`` and can
never reach the DB), path/infra bootstrap that the settings module itself needs
(``workspace_dir``, ``worktrees_dir``, ``timezone``,
``privacy``), and nested structured tables that have no flat scalar shape for a
``ConfigSetting`` row (``mr_reminder``). Every other field is DB-home — including
the ~32 that are file-only today.

:data:`DERIVED_FIELDS` are the two values the resolver COMPUTES rather than
reads (``notify_on_behalf`` derived by the autonomy collapse,
``ask_before_post_on_behalf`` derived from ``on_behalf_post_mode``); they have
no home and are excluded from the partition.

The fitness functions in ``tests/config/test_settings_home_partition.py`` keep
this exhaustive and disjoint: every ``UserSettings`` field is in exactly one of
:data:`SETTING_HOMES` / :data:`DERIVED_FIELDS`, and the two homes never overlap.
"""

from enum import StrEnum


class SettingHome(StrEnum):
    """The single authoritative tier of a ``UserSettings`` field."""

    DB = "db"
    TOML = "toml"


# The two values the resolver computes rather than reads — no home, excluded
# from the partition. ``notify_on_behalf`` is ORed in by the autonomy collapse;
# ``ask_before_post_on_behalf`` is derived from the resolved ``on_behalf_post_mode``.
DERIVED_FIELDS: frozenset[str] = frozenset({"notify_on_behalf", "ask_before_post_on_behalf"})

# The irreducible TOML-home carve-out (exactly these eleven):
# - non-Django / pre-Django readers (read via tomllib or a bash grep, no DB):
#   ``orchestrator_bash_gate_enabled``, ``speak``, ``handover_mirror_path``,
#   ``check_updates``, and ``statusline_chain`` (the bash statusline hook reads
#   ``[teatree] statusline_chain`` straight from ``~/.teatree.toml`` — it has no
#   path to the Django DB, so a DB row for it would be silently unread)
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
