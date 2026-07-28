"""Model-driven, secret-safe settings editor surface for the dashboard (D7).

Walks the pydantic schema (``TeatreeSettingsSchema``) so every config key is listable
and editable with NO hand-kept list — a newly-added setting appears here for free. The
edit path writes through ``ConfigSetting.set_value`` (the same seam ``config_setting set``
uses), so the #258 strict coercion and the #3688 cross-key checks fire identically.

**One section at a time.** The page is a left nav of sections and a right pane holding the
selected section's rows, so a request renders ~10-25 rows rather than every key at once.
:func:`build_settings_sections` is the nav (derived from the group tree over the key names,
so it costs no row building) and :func:`build_settings_group` is the pane. The two come off
the SAME tree, so a section the nav offers always has a pane, and the union of the panes is
the whole schema — the never-drop guarantee the retired band classifier failed, now held
across sections instead of within one page.

**A secret value never reaches the response.** :func:`~teatree.core.config_display.is_secret`
(the shared value-masking taxonomy — ``Category.SECRET`` / ``SECRET_SETTINGS`` / credential
coordinate / personal identifier) drives masking here AND on the read-only config surface,
so the two pages apply ONE policy. A secret row's value AND its shipped default are replaced
with ``***`` HERE, before the row enters the view context — so the stored value is never read
into a rendered string. Restore-to-default deletes the DB row (the Phase-4 zero-row /
``restore = delete row`` semantics). Export withholds secrets and keeps personal; import
previews via ``import_toml_to_db(dry_run=True)``.

Every row carries WHERE its effective value came from (``teatree.config.provenance``) beside
the shipped default the file carries and whether the two still agree. That replaces the old
``category`` column, which showed the setting's KIND — ``default`` for hundreds of rows
running, read as "this value came from the default" while the column beside it said the
value differs from that default.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.provenance import ResolvedSetting, ValueSource, resolve_settings
from teatree.config.schema import TeatreeSettingsSchema
from teatree.config.setting_groups import SettingGroupNode, group_leaves, group_slug, group_tree
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.core.config_display import MASKED, is_secret, render_value
from teatree.core.config_migration import ConfigImport, export_db_to_toml, import_toml_to_db
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import GLOBAL_SCOPE, ConfigValue
from teatree.core.overlay_loader import get_all_overlays

logger = logging.getLogger(__name__)

SAME_AS_DEFAULT = "same as default"
DIFFERS_FROM_DEFAULT = "differs from default"


@dataclass(frozen=True, slots=True)
class EditableSetting:
    """One row of the editor — its value and its shipped default already masked when secret."""

    name: str
    value: str  # ``***`` for a secret, else the effective value as display text
    source: str  # which tier supplied the value — env / DB scope / code default / shipped file
    is_secret: bool
    is_safety_posture: bool
    is_overridden: bool  # an operator tier supplied the value → restore-to-default applies
    shipped_default: str  # ``***`` for a secret, "" when the shipped file carries none
    has_shipped_default: bool
    matches_shipped_default: bool
    default_comparison: str  # the words the colour accompanies; "" when there is no default


@dataclass(frozen=True, slots=True)
class SettingsSection:
    """One entry of the left nav — a leaf group, addressable on its own."""

    label: str
    path: tuple[str, ...]
    slug: str
    key_count: int

    @property
    def parent_label(self) -> str:
        """The levels above this leaf, for a nav that shows where a section sits."""
        return " / ".join(self.path[:-1])


@dataclass(frozen=True, slots=True)
class SettingsGroupView:
    """The right pane — one section's rows, or a visible error (never a 500)."""

    section: SettingsSection | None = None
    settings: tuple[EditableSetting, ...] = ()
    scope: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class SettingsEditorView:
    """The page frame — the nav, the selected section's pane, and the scope picker."""

    sections: tuple[SettingsSection, ...] = ()
    group: SettingsGroupView = SettingsGroupView()
    scope: str = ""
    available_scopes: tuple[str, ...] = ("",)
    error: str = ""


def _display_value(key: str, resolved: ResolvedSetting) -> str:
    """The row's shown value — ``***`` for a secret, else the resolved value as text.

    A secret returns ``MASKED`` WITHOUT reading the resolved value, so a stored secret can
    never be serialised into the page.
    """
    return MASKED if is_secret(key) else render_value(resolved.value)


def _display_default(key: str, shipped: Mapping[str, ConfigValue]) -> str:
    """The shipped default as display text — ``***`` for a secret, "" when there is none.

    Read from the shipped TABLE, in the same stored form the value column renders, so
    "same as default" is a comparison of like with like rather than of a stored scalar
    against a coerced dataclass value.
    """
    if is_secret(key):
        return MASKED
    return render_value(shipped[key]) if key in shipped else ""


def _row(key: str, resolved: ResolvedSetting, shipped: Mapping[str, ConfigValue]) -> EditableSetting:
    has_default = key in shipped
    value = _display_value(key, resolved)
    default = _display_default(key, shipped)
    # Compared as the operator SEES them: identical text on the row means identical value.
    matches = not resolved.is_overridden or value == default
    return EditableSetting(
        name=key,
        value=value,
        source=resolved.source.value,
        is_secret=is_secret(key),
        is_safety_posture=key in SAFETY_POSTURE_KEYS,
        is_overridden=resolved.is_overridden,
        shipped_default=default,
        has_shipped_default=has_default,
        matches_shipped_default=matches,
        default_comparison=(SAME_AS_DEFAULT if matches else DIFFERS_FROM_DEFAULT) if has_default else "",
    )


def _leaves() -> tuple[SettingGroupNode[str], ...]:
    """The schema's key names partitioned into the group tree's leaves, in render order."""
    return group_leaves(group_tree(sorted(TeatreeSettingsSchema.model_fields), key_of=lambda key: key))


def build_settings_sections() -> tuple[SettingsSection, ...]:
    """The left nav — every leaf group of the hierarchy, in the tree's own order.

    Built from the key NAMES alone, so listing the nav costs no value resolution. The
    partition is total, so every schema key is reachable through exactly one entry.
    """
    return tuple(
        SettingsSection(label=leaf.label, path=leaf.path, slug=group_slug(leaf.path), key_count=len(leaf.rows))
        for leaf in _leaves()
    )


def build_settings_group(slug: str = "", scope: str = "") -> SettingsGroupView:
    """One section's editable rows; the first section when *slug* names none.

    Only this section's keys are resolved, which is what keeps the page one section wide
    rather than the whole schema deep.
    """
    leaves = {group_slug(leaf.path): leaf for leaf in _leaves()}
    leaf = leaves.get(slug) or next(iter(leaves.values()), None)
    if leaf is None:
        return SettingsGroupView(scope=scope, error="settings unavailable — the schema declares no groups")
    section = SettingsSection(label=leaf.label, path=leaf.path, slug=group_slug(leaf.path), key_count=len(leaf.rows))
    try:
        shipped = shipped_defaults_table()
        resolved = resolve_settings(leaf.rows, scope=scope)
    except Exception:
        logger.warning("dash settings group read failed — degrading to an error pane", exc_info=True)
        return SettingsGroupView(section=section, scope=scope, error="settings unavailable — read failed")
    rows = tuple(_row(key, resolved[key], shipped) for key in leaf.rows)
    return SettingsGroupView(section=section, settings=rows, scope=scope)


def build_settings_editor(slug: str = "", scope: str = "") -> SettingsEditorView:
    """Compose the whole page — the nav, the selected pane, the scope picker."""
    try:
        sections = build_settings_sections()
        scopes = available_scopes()
    except Exception:
        logger.warning("dash settings editor read failed — degrading to an error page", exc_info=True)
        return SettingsEditorView(scope=scope, error="settings unavailable — read failed")
    group = build_settings_group(slug, scope)
    return SettingsEditorView(sections=sections, group=group, scope=scope, available_scopes=scopes)


def available_scopes() -> tuple[str, ...]:
    """Global first, then every overlay scope the operator can edit.

    The union of the registered overlays and the scopes that already hold rows, so a
    scope written by ``config_setting set --overlay`` before its overlay was registered
    (or after it was uninstalled) is still reachable rather than stranded.
    """
    stored = ConfigSetting.objects.exclude(scope=GLOBAL_SCOPE).values_list("scope", flat=True).distinct()
    try:
        registered = get_all_overlays().keys()
    except Exception:
        logger.warning("overlay discovery failed — offering only the scopes holding rows", exc_info=True)
        registered = ()
    return (GLOBAL_SCOPE, *sorted({*stored, *registered}))


def build_setting_row(key: str, scope: str = "") -> EditableSetting:
    """One row, re-read after a write — the htmx swap unit, masked by the same policy."""
    return _row(key, resolve_settings([key], scope=scope)[key], shipped_defaults_table())


def export_text(*, default_keys_only: bool = False, include_defaults: bool = False) -> str:
    """The shareable export dump — secrets withheld, personal kept (Phase-4 semantics).

    The two filters are the page's two checkboxes, both unticked by default so the plain
    download is the delta dump it has always been. Ticking both yields the ``defaults.toml``
    shape: a complete, drop-in replacement for the shipped file.
    """
    return export_db_to_toml(
        include_private=False,
        default_keys_only=default_keys_only,
        include_defaults=include_defaults,
    ).toml


def import_preview(text: str) -> ConfigImport:
    """Classify an import WITHOUT writing — the dry-run preview of what would change.

    Classifies as if the safety-posture keys were authorized so the preview can SHOW and flag
    them; nothing is written, and the apply path re-runs the classification with the operator's
    actual authorization.
    """
    return import_toml_to_db(text, dry_run=True, allow_safety_posture=True)


__all__ = [
    "MASKED",
    "EditableSetting",
    "SettingsEditorView",
    "SettingsGroupView",
    "SettingsSection",
    "ValueSource",
    "available_scopes",
    "build_setting_row",
    "build_settings_editor",
    "build_settings_group",
    "build_settings_sections",
    "export_text",
    "import_preview",
]
