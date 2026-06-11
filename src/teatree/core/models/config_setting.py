"""DB-backed config override store — the canonical override tier (#1775).

The first concrete slice of "move config to the database": a generic key/value
row that overrides the file/env config for a single setting, reusing the
established "canonical tier is the DB with file/env fallback" pattern
(``MergeClear`` / ``DbApproval``, BLUEPRINT §17.4 / #953).

The contract is intentionally narrow so an **empty table is a provable no-op**.
:meth:`ConfigSettingManager.get_effective` returns the stored value when a row
exists for *key*, else ``None`` — and ``None`` means "no DB override, fall
through to the file/env source". The resolver
(``teatree.config.resolution.get_effective_settings``) consults this between the
env layer (which still wins) and the per-overlay TOML layer, so the documented
precedence becomes env → DB → per-overlay TOML → global ``[teatree]`` →
dataclass default. The ``value`` is a ``JSONField`` so any TOML-shaped
scalar/list/dict round-trips (bool kill-switch, label string, int budget, list).

Bootstrap-readable settings (``DATABASE_URL`` / data-dir /
``DJANGO_SETTINGS_MODULE`` / the offline ``private_repos`` allowlist) are
explicitly out of scope — they must be readable before Django starts, so they can
never live here (#1775).
"""

from typing import ClassVar

from django.db import models

# Any TOML/JSON-shaped value a setting may hold. Recursive in principle
# (lists/dicts nest), but the override registry only ever coerces scalars and
# flat lists, so the flat union is the honest, lint-clean alias (avoids ANN401's
# `Any`). ``None`` is NOT included — absence is the fall-through sentinel, and
# the pilot never stores a JSON null (see the manager docstring).
type ConfigValue = bool | int | float | str | list[object] | dict[str, object]


class ConfigSettingManager(models.Manager["ConfigSetting"]):
    """Read/write helpers for the DB override tier.

    The manager is the resolver's single entry point: it owns the
    absent-key → ``None`` fall-through contract and the upsert/clear admin
    operations, keeping the resolver (a different tach layer) free of any
    knowledge beyond "ask the manager".
    """

    def get_effective(self, key: str) -> ConfigValue | None:
        """Return the stored value for *key*, or ``None`` when no row exists.

        ``None`` is the fall-through sentinel: the resolver interprets it as
        "no DB override for this setting" and keeps the file/env value. An
        empty table therefore leaves every setting resolving exactly as it does
        today — the #1775 no-regression-during-migration invariant.
        """
        row = self.filter(key=key).first()
        return row.value if row is not None else None

    def set_value(self, key: str, value: ConfigValue) -> "ConfigSetting":
        """Upsert the override row for *key* to *value* (single-use admin path).

        The unique ``key`` makes this an idempotent upsert: setting the same
        key twice updates the one row rather than creating a duplicate.
        """
        row, _ = self.update_or_create(key=key, defaults={"value": value})
        return row

    def clear(self, key: str) -> bool:
        """Delete the override row for *key*; return whether a row was removed.

        After ``clear`` the setting falls back through to the file/env source
        (``get_effective`` returns ``None`` again).
        """
        deleted, _ = self.filter(key=key).delete()
        return deleted > 0


class ConfigSetting(models.Model):
    """One DB-backed override of a ``UserSettings`` field, keyed by its name.

    The ``key`` is the canonical ``UserSettings`` field name (e.g.
    ``issue_implementer_enabled``) — the same string used in
    ``OVERLAY_OVERRIDABLE_SETTINGS``. The ``value`` is stored as JSON so any
    TOML-shaped value round-trips. ``key`` is unique so the manager's
    ``set_value`` is a clean upsert.
    """

    key = models.CharField(max_length=255, unique=True)
    value = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[ConfigSettingManager] = ConfigSettingManager()

    class Meta:
        db_table = "teatree_config_setting"
        ordering: ClassVar = ["key"]

    def __str__(self) -> str:
        return f"config-setting<{self.key}={self.value!r}>"
