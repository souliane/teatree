"""Model-driven, secret-safe settings editor surface for the dashboard (D7).

Walks the pydantic schema (``TeatreeSettingsSchema``) so every config key is listable
and editable with NO hand-kept list — a newly-added setting appears here for free. The
edit path writes through ``ConfigSetting.set_value`` (the same seam ``config_setting set``
uses), so the #258 strict coercion and the #3688 cross-key checks fire identically.

**A secret value never reaches the response.** ``is_secret_setting`` reuses the export
withhold taxonomy (``Category.SECRET`` / ``SECRET_SETTINGS`` / credential coordinate /
personal identifier); a secret row's value AND its shipped default are replaced with
``***`` HERE, before the row enters the view context — so the stored value is never read
into a rendered string. Restore-to-default deletes the DB row (the Phase-4 zero-row /
``restore = delete row`` semantics). Export withholds secrets and keeps personal; import
previews via ``import_toml_to_db(dry_run=True)``.
"""

import logging
from dataclasses import dataclass

from teatree.config.schema import Category, TeatreeSettingsSchema, setting_meta, shipped_defaults
from teatree.config.secret_settings import PERSONAL_IDENTIFIERS, SECRET_SETTINGS, is_credential_reference
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.core.config_migration import ConfigImport, export_db_to_toml, import_toml_to_db
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import ConfigValue

logger = logging.getLogger(__name__)

#: Rendered in place of a secret value AND its default — never the real value.
MASKED = "***"


def is_secret_setting(key: str) -> bool:
    """Whether *key*'s value must never be rendered — the export withhold taxonomy.

    The union the export secret guard withholds by key: a ``Category.SECRET`` field, an
    explicit ``SECRET_SETTINGS`` denylist key, a credential coordinate (``*_pass_path`` /
    ``*_token_ref`` / …), or a personal identifier (``slack_user_id`` / …). Total over any
    string — the key-based classes are checked before the model, so a denylist key that is
    not a schema field (a legacy token ref) is still classified without a lookup that raises.
    """
    if key in SECRET_SETTINGS or key in PERSONAL_IDENTIFIERS or is_credential_reference(key):
        return True
    if key not in TeatreeSettingsSchema.model_fields:
        return False
    return setting_meta(key).category is Category.SECRET


@dataclass(frozen=True, slots=True)
class EditableSetting:
    """One row of the editor — its value already masked when secret."""

    name: str
    category: str  # "default" / "personal" / "secret"
    value: str  # ``***`` for a secret, else the effective value as display text
    is_secret: bool
    is_safety_posture: bool
    is_overridden: bool  # a DB row exists in this scope → restore-to-default is available


@dataclass(frozen=True, slots=True)
class SettingsEditorView:
    """The whole editor page — every setting, or a visible error (never a 500)."""

    settings: tuple[EditableSetting, ...] = ()
    scope: str = ""
    error: str = ""


def _render(value: object) -> str:
    """A value as display text — booleans as on/off, empties as a dash."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None or (isinstance(value, str) and not value) or value in ([], {}):
        return "—"
    return str(value)


def _display_value(key: str, *, overridden: bool, db_value: ConfigValue | None) -> str:
    """The row's shown value — ``***`` for a secret (its default too), else DB-or-default.

    A secret returns ``MASKED`` WITHOUT reading the stored value or the shipped default, so
    neither can ever be serialised into the page.
    """
    if is_secret_setting(key):
        return MASKED
    value = db_value if overridden else getattr(shipped_defaults(), key)
    return _render(value)


def build_settings_editor(scope: str = "") -> SettingsEditorView:
    """Compose the editable row for every schema key in *scope*; degrade to a visible error."""
    try:
        overrides = ConfigSetting.objects.overrides_for_scope(scope)
        rows = [
            EditableSetting(
                name=key,
                category=setting_meta(key).category.value,
                value=_display_value(key, overridden=key in overrides, db_value=overrides.get(key)),
                is_secret=is_secret_setting(key),
                is_safety_posture=key in SAFETY_POSTURE_KEYS,
                is_overridden=key in overrides,
            )
            for key in sorted(TeatreeSettingsSchema.model_fields)
        ]
    except Exception:
        logger.warning("dash settings editor read failed — degrading to an error page", exc_info=True)
        return SettingsEditorView(scope=scope, error="settings unavailable — read failed")
    return SettingsEditorView(settings=tuple(rows), scope=scope)


def export_text() -> str:
    """The shareable export dump — secrets withheld, personal kept (Phase-4 semantics)."""
    return export_db_to_toml(include_private=False).toml


def import_preview(text: str) -> ConfigImport:
    """Classify an import WITHOUT writing — the dry-run preview of what would change."""
    return import_toml_to_db(text, dry_run=True)


__all__ = [
    "MASKED",
    "EditableSetting",
    "SettingsEditorView",
    "build_settings_editor",
    "export_text",
    "import_preview",
    "is_secret_setting",
]
