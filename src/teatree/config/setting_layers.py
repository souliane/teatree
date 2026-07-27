"""The tier LAYERS of one settings resolution — their shape, and how each folds in.

Split out of ``resolution`` for the module-health LOC cap: ``resolution`` owns the
resolution ORDER (which tier reads from where, the pin sets, the autonomy collapse),
this module owns what ONE layer is and how a layer folds onto the one below it.

:class:`SettingLayers` is the ascending-precedence stored-form tuple every resolution
reads once. :func:`apply_structured_settings` folds the two nested-table fields
(``speak`` / ``mr_reminder``) that a flat ``dataclasses.replace`` cannot carry.
:func:`drop_db_home_overlay_keys` filters the per-overlay overlays-registry override
layer by the #1775 home partition.

Django-free and pydantic-free — ``teatree.config``'s package init imports
``resolution``, which imports this, and the cold hook path imports that package.
"""

import logging
from dataclasses import dataclass, replace
from typing import Any

from teatree.config.homes import SETTING_HOMES, SettingHome
from teatree.config.mr_reminder import mr_reminder_from_table
from teatree.config.settings import UserSettings
from teatree.config.speak import speak_from_subtable
from teatree.types import SpeakConfig

_logger = logging.getLogger("teatree.config")


@dataclass(frozen=True)
class SettingLayers:
    """The stored-form tiers of one resolution, read ONCE and served in both forms.

    ``raw`` is the ascending-precedence ``(TOML default, DB global, DB overlay)`` tuple
    :func:`apply_structured_settings` walks (``speak`` / ``mr_reminder`` are JSON dicts
    the row coercer skips); the three coerced maps drive the generic
    ``dataclasses.replace``.
    """

    raw: tuple[dict[str, Any], ...]
    toml_defaults: dict[str, Any]
    global_db: dict[str, Any]
    overlay_db: dict[str, Any]


def apply_structured_settings(
    settings: UserSettings,
    row_layers: tuple[dict[str, Any], ...],
    base_speak: SpeakConfig,
) -> UserSettings:
    """Resolve the nested-table fields from *row_layers*, LOWEST precedence first (#1775).

    The layers are ``(TOML default, DB global, DB overlay)``. ``mr_reminder`` takes the
    HIGHEST layer that carries a table (no merge — it has no per-scope merge layer).
    ``speak`` is the one non-generic override: each layer MERGES onto the one below it
    (:func:`speak_from_subtable`), so a partial row overrides only the keys it sets.
    """
    mr_tables = [layer["mr_reminder"] for layer in row_layers if isinstance(layer.get("mr_reminder"), dict)]
    if mr_tables:
        settings = replace(settings, mr_reminder=mr_reminder_from_table(mr_tables[-1]))
    speak = _merge_speak_layers(row_layers, base_speak)
    if speak is not None:
        settings = replace(settings, speak=speak)
    return settings


def _merge_speak_layers(row_layers: tuple[dict[str, Any], ...], base: SpeakConfig) -> SpeakConfig | None:
    """Merge each layer's ``speak`` table onto the one below it (#2050 semantics).

    ``None`` (no ``speak`` table in any layer) → the dataclass default stands.
    """
    merged = base
    found = False
    for layer in row_layers:
        table = layer.get("speak")
        if isinstance(table, dict):
            merged = speak_from_subtable(table, base=merged)
            found = True
    return merged if found else None


def toml_home(key: str) -> bool:
    """Whether *key* is a TOML-home ``UserSettings`` field (#1775 partition).

    The TOML-home carve-out is currently EMPTY (the per-install file config tier was
    removed), so this returns ``False`` for every live key — the per-overlay
    overlays-registry override layer then drops it, since its authoritative override
    tier is the ``ConfigSetting`` store, never the ``[overlays.<name>]`` registry entry.
    Kept as the carve-out predicate so a future file-tier re-introduction is a
    deliberate, tested change (``config/homes.py``). Orthogonal to the shipped
    ``defaults.toml`` DEFAULTS tier, which is a base under every field's overrides.
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
    makes the no-op visible. With the TOML carve-out empty, every override key is
    DB-home, so this keeps nothing and returns ``{}``.

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
