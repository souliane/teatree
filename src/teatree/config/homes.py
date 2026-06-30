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

The TOML-home set is the carve-out — a field stays here ONLY when a NON-DJANGO /
PRE-DJANGO reader needs it (the DB is then unreachable) or it is a nested table with
no flat ``ConfigSetting`` shape. The pre-Django readers: ``orchestrator_bash_gate_enabled``
(the GATE_KEY bash self-rescue), ``speak`` (the Stop hook re-reads ``[teatree.speak]``
with tomllib), ``handover_mirror_path`` (the SessionStart bootstrap path read when the
DB is unreachable), ``autoload`` (the cold SessionStart / UserPromptSubmit hooks read
``[teatree] autoload`` pre-Django to decide engagement, #256), ``privacy`` (the
pre-Django MCP privacy gate), and ``statusline_chain`` (the bash statusline hook reads it
straight from ``~/.teatree.toml``). The nested structured table with no flat
scalar shape: ``mr_reminder``. Every other field is DB-home — it resolves from the
``ConfigSetting`` store + env, never from a ``[teatree]`` / ``[overlays.<name>]`` TOML
value (which is ignored on read and the resolver warns on). ``workspace_dir`` and
``worktrees_dir`` are DB-home (resolved Django-side off the store — Django ``settings.py``
hardcodes ``TIME_ZONE`` and configures ``DATABASES`` without reading either, so neither
was ever a DB-open bootstrap dep); ``timezone`` is DB-home too (no live reader).

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

# The TOML-home carve-out (exactly these seven):
# - non-Django / pre-Django readers (read via tomllib or a bash grep, no DB):
#   ``orchestrator_bash_gate_enabled`` (the GATE_KEY bash self-rescue), ``speak``
#   (the Stop hook re-reads the ``[teatree.speak]`` sub-table with tomllib — it
#   cannot reach the Django DB), ``handover_mirror_path`` (the SessionStart
#   bootstrap path read precisely when the DB is unreachable), ``autoload`` (the
#   cold SessionStart / UserPromptSubmit hooks read ``[teatree] autoload`` with
#   tomllib to decide default-off engagement, before any Django bootstrap — #256),
#   ``privacy`` (the pre-Django MCP privacy gate), and ``statusline_chain`` (the
#   bash statusline hook reads ``[teatree] statusline_chain`` straight from
#   ``~/.teatree.toml``).
# - nested structured table with no flat ConfigSetting shape: ``mr_reminder``
#
# eliminate-~/.teatree.toml LEFT the carve-out: ``check_updates`` (cold_reader on
# its pre-Django path), and ``worktrees_dir`` / ``timezone`` — tagged "needed to
# open the DB" but Django ``settings.py`` hardcodes ``TIME_ZONE = "UTC"`` and
# configures ``DATABASES`` without reading either, so neither was a bootstrap dep.
#
# ``workspace_dir`` / ``worktrees_dir`` are DB-home (resolved Django-side off the
# ``ConfigSetting`` store): worktrees regroup under a per-overlay default
# ``~/workspace/t3-workspaces/<overlay>/``, resolved by ``config.worktree_root()``
# (env → DB overlay-scope → DB global-scope → default). Read only after Django is
# up, so no bootstrap need. ``workspace_dir`` is distinct from the CLONE root
# ``config.clone_root()`` (``~/workspace``, where main repo clones live).
_TOML_HOME: frozenset[str] = frozenset(
    {
        "orchestrator_bash_gate_enabled",
        "speak",
        "mr_reminder",
        "handover_mirror_path",
        "autoload",
        "statusline_chain",
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
