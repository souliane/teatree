"""The tier LAYERS of one settings resolution — their shape, and how each folds in.

Split out of ``resolution`` for the module-health LOC cap: ``resolution`` owns the
resolution ORDER (which tier reads from where, the pin sets, the autonomy collapse),
this module owns what ONE layer is and how a layer folds onto the one below it.

:class:`SettingLayers` is the ascending-precedence stored-form tuple every resolution
reads once. :func:`apply_structured_settings` folds the two nested-table fields
(``mr_reminder``) that a flat ``dataclasses.replace`` cannot carry.
:func:`drop_db_home_overlay_keys` filters the per-overlay overlays-registry override
layer by the #1775 home partition.

Django-free and pydantic-free — ``teatree.config``'s package init imports
``resolution``, which imports this, and the cold hook path imports that package.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from teatree.config.homes import SETTING_HOMES, SettingHome
from teatree.config.mr_reminder import mr_reminder_from_table
from teatree.config.settings import UserSettings

_logger = logging.getLogger("teatree.config")


@dataclass(frozen=True)
class SettingLayers:
    """The stored-form tiers of one resolution, read ONCE and served in both forms.

    ``db_rows`` is the ascending-precedence ``(global, overlay)`` tuple
    :func:`apply_structured_settings` walks (``mr_reminder`` is a JSON dict
    the row coercer skips), and ``global_db`` / ``overlay_db`` are those same two rows
    coerced, driving the generic ``dataclasses.replace``.
    """

    db_rows: tuple[dict[str, Any], ...]
    global_db: dict[str, Any]
    overlay_db: dict[str, Any]
    overlay_scopes: dict[str, str] = field(default_factory=dict)
    #: The DB scopes whose override read FAILED — ``"global"`` and/or ``"overlay"`` (#3873).
    #: Empty on a healthy read, INCLUDING a healthy read of an empty table: a scope is
    #: listed only when the tier could not be determined, never when it is merely absent.
    #: That distinction is the whole point — ``{}`` alone cannot carry it.
    degraded_scopes: frozenset[str] = frozenset()


def apply_structured_settings(
    settings: UserSettings,
    row_layers: tuple[Mapping[str, Any], ...],
) -> UserSettings:
    """Resolve the nested-table fields from *row_layers*, LOWEST precedence first (#1775).

    ``mr_reminder`` takes the HIGHEST layer that carries a table (no merge — it has no
    per-scope merge layer).
    """
    mr_tables = [layer["mr_reminder"] for layer in row_layers if isinstance(layer.get("mr_reminder"), dict)]
    if mr_tables:
        settings = replace(settings, mr_reminder=mr_reminder_from_table(mr_tables[-1]))
    return settings


def toml_home(key: str) -> bool:
    """Whether *key* is a TOML-home ``UserSettings`` field (#1775 partition).

    The per-install file config tier was removed, so every ``UserSettings`` field is
    DB-home (``config/homes.py``) and this returns ``False`` for every live key: the
    per-overlay overlays-registry override layer drops it, since a field's
    authoritative override tier is the ``ConfigSetting`` store, never the
    ``[overlays.<name>]`` registry entry. Orthogonal to the shipped ``defaults.toml``
    DEFAULTS tier, which is a base under every field's overrides.
    """
    return SETTING_HOMES.get(key) is SettingHome.TOML


def drop_db_home_overlay_keys(overrides: dict[str, Any], overlay_name: str) -> dict[str, Any]:
    """Keep only TOML-home override keys, WARNING loud on each dropped DB-home key.

    The footgun the warning closes (the silent-drop the maintainer flagged): a DB
    overlays-registry entry (``[overlays.<name>]`` in ``config.raw``) carries a
    DB-home key (e.g. ``mode = "auto"``) that the operator expects to take effect,
    but a DB-home field's sole override home is the ``ConfigSetting`` store — so the
    resolver drops the registry value. With NO DB row beneath it the dropped value also
    has no effect, and nothing told the operator their override was ignored. Surfacing
    the drop loud (one aggregated WARN naming every dropped key and the migration path)
    makes the no-op visible. Every ``UserSettings`` field is DB-home, so this keeps
    nothing and returns ``{}``.

    Unknown keys (not in the home registry at all) are NOT warned — a stray key is
    a different concern; only a genuine DB-home ``UserSettings`` field flagged here.
    """
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in overrides.items():
        if toml_home(key):
            kept[key] = value
        elif SETTING_HOMES.get(key) is SettingHome.DB:
            dropped.append(key)
    if dropped:
        scope = overlay_name or "(active overlay)"
        _logger.warning(
            "Config override keys for overlay %s are DB-home settings, so a stray non-DB value is "
            "IGNORED on read and had NO effect: %s. Their authoritative home is the ConfigSetting "
            "store — set them with `t3 <overlay> config_setting set <key> <value> --overlay %s`.",
            scope,
            ", ".join(sorted(dropped)),
            scope,
        )
    return kept
