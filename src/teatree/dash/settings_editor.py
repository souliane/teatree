"""Model-driven, secret-safe settings editor surface for the dashboard (D7).

Walks the pydantic schema (``TeatreeSettingsSchema``) so every config key is listable
and editable with NO hand-kept list — a newly-added setting appears here for free. The
edit path writes through ``ConfigSetting.set_value`` (the same seam ``config_setting set``
uses), so the #258 strict coercion and the #3688 cross-key checks fire identically.

**A secret value never reaches the response.** :func:`~teatree.core.config_display.is_secret`
(the shared value-masking taxonomy — ``Category.SECRET`` / ``SECRET_SETTINGS`` / credential
coordinate / personal identifier) drives masking here AND on the read-only config surface,
so the two pages apply ONE policy. A secret row's value AND its shipped default are replaced
with ``***`` HERE, before the row enters the view context — so the stored value is never read
into a rendered string. Restore-to-default deletes the DB row (the Phase-4 zero-row /
``restore = delete row`` semantics). Export withholds secrets and keeps personal; import
previews via ``import_toml_to_db(dry_run=True)``.

Every row also carries the shipped default `config/defaults.toml` ships for that key and
whether the effective value still matches it. A Secret/Personal key is absent from that
file by construction, so it has no default to compare against and offers no verdict.

Rows are rendered in :func:`~teatree.config.setting_groups.setting_group` groups, and the
grouping is TOTAL: :func:`group_rows` partitions the flat row set, so a key whose group is
unknown lands in the leftovers bucket rather than being dropped — the defect the retired
band classifier shipped, where 130 of 184 keys classified to ``""`` and were skipped.
"""

import logging
from dataclasses import dataclass

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.schema import TeatreeSettingsSchema, setting_meta, shipped_defaults
from teatree.config.setting_groups import UNGROUPED_LABEL, group_labels, setting_group
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
    category: str  # "default" / "personal" / "secret"
    value: str  # ``***`` for a secret, else the effective value as display text
    is_secret: bool
    is_safety_posture: bool
    is_overridden: bool  # a DB row exists in this scope → restore-to-default is available
    shipped_default: str  # ``***`` for a secret, "" when the shipped file carries none
    has_shipped_default: bool
    matches_shipped_default: bool
    default_comparison: str  # the words the colour accompanies; "" when there is no default


@dataclass(frozen=True, slots=True)
class SettingGroupView:
    """One rendered group — its label and the rows that declared themselves into it."""

    label: str
    settings: tuple[EditableSetting, ...]
    is_ungrouped: bool


@dataclass(frozen=True, slots=True)
class SettingsEditorView:
    """The whole editor page — every setting, or a visible error (never a 500).

    ``settings`` is the flat authoritative row set; ``groups`` is a partition of it, so
    the two can never disagree about which keys the page carries.
    """

    settings: tuple[EditableSetting, ...] = ()
    groups: tuple[SettingGroupView, ...] = ()
    scope: str = ""
    available_scopes: tuple[str, ...] = ("",)
    error: str = ""


def _display_value(key: str, *, overridden: bool, db_value: ConfigValue | None) -> str:
    """The row's shown value — ``***`` for a secret (its default too), else DB-or-default.

    A secret returns ``MASKED`` WITHOUT reading the stored value or the shipped default, so
    neither can ever be serialised into the page.
    """
    if is_secret(key):
        return MASKED
    value = db_value if overridden else getattr(shipped_defaults(), key)
    return render_value(value)


def _display_default(key: str, *, has_default: bool) -> str:
    """The shipped default as display text — ``***`` for a secret, "" when there is none.

    A secret is masked BEFORE the value is read, exactly as :func:`_display_value` does, so
    neither the stored value nor the shipped default can be serialised into the page.
    """
    if is_secret(key):
        return MASKED
    return render_value(getattr(shipped_defaults(), key)) if has_default else ""


def _row(key: str, overrides: dict[str, ConfigValue], shipped_keys: frozenset[str]) -> EditableSetting:
    overridden = key in overrides
    has_default = key in shipped_keys
    value = _display_value(key, overridden=overridden, db_value=overrides.get(key))
    default = _display_default(key, has_default=has_default)
    # Compared as the operator SEES them: identical text on the row means identical value.
    # An unset key resolves FROM the default, so it matches by construction.
    matches = not overridden or value == default
    return EditableSetting(
        name=key,
        category=setting_meta(key).category.value,
        value=value,
        is_secret=is_secret(key),
        is_safety_posture=key in SAFETY_POSTURE_KEYS,
        is_overridden=overridden,
        shipped_default=default,
        has_shipped_default=has_default,
        matches_shipped_default=matches,
        default_comparison=(SAME_AS_DEFAULT if matches else DIFFERS_FROM_DEFAULT) if has_default else "",
    )


def group_rows(rows: tuple[EditableSetting, ...]) -> tuple[SettingGroupView, ...]:
    """Partition *rows* into rendered groups — total by construction, so no row is dropped.

    A row whose group is unknown collects in :data:`UNGROUPED_LABEL`, which renders only
    when it has members. An empty declared group is omitted; the leftovers bucket is not,
    because its whole job is to be seen.
    """
    buckets: dict[str, list[EditableSetting]] = {label: [] for label in group_labels()}
    for row in rows:
        buckets.setdefault(setting_group(row.name) or UNGROUPED_LABEL, []).append(row)
    return tuple(
        SettingGroupView(label=label, settings=tuple(members), is_ungrouped=label == UNGROUPED_LABEL)
        for label, members in buckets.items()
        if members
    )


def build_settings_editor(scope: str = "") -> SettingsEditorView:
    """Compose the editable row for every schema key in *scope*; degrade to a visible error."""
    try:
        overrides = ConfigSetting.objects.overrides_for_scope(scope)
        shipped_keys = frozenset(shipped_defaults_table())
        rows = tuple(_row(key, overrides, shipped_keys) for key in sorted(TeatreeSettingsSchema.model_fields))
        scopes = available_scopes()
    except Exception:
        logger.warning("dash settings editor read failed — degrading to an error page", exc_info=True)
        return SettingsEditorView(scope=scope, error="settings unavailable — read failed")
    return SettingsEditorView(settings=rows, groups=group_rows(rows), scope=scope, available_scopes=scopes)


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
    return _row(key, ConfigSetting.objects.overrides_for_scope(scope), frozenset(shipped_defaults_table()))


def export_text() -> str:
    """The shareable export dump — secrets withheld, personal kept (Phase-4 semantics)."""
    return export_db_to_toml(include_private=False).toml


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
    "SettingGroupView",
    "SettingsEditorView",
    "available_scopes",
    "build_setting_row",
    "build_settings_editor",
    "export_text",
    "group_rows",
    "import_preview",
]
