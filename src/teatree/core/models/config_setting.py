"""DB-backed config override store — the canonical override tier (#1775).

The first concrete slice of "move config to the database": a generic key/value
row that overrides the file/env config for a single setting, reusing the
established "canonical tier is the DB with file/env fallback" pattern
(``MergeClear`` / ``DbApproval``, BLUEPRINT §17.4 / #953).

The contract is intentionally narrow so an **empty table is a provable no-op**.
:meth:`ConfigSettingManager.get_effective` returns the stored value when a row
exists for *key*, else ``None`` — and ``None`` means "no row, fall through to the
dataclass default". Under the #1775 hard partition this store is the SOLE
authoritative tier for a DB-home field (plus the ``T3_*`` env layer, which still
wins): ``[teatree]`` / ``[overlays.<name>]`` TOML is not read for a DB-home key,
so the per-field precedence is env → DB(overlay scope) → DB(global scope) →
dataclass default. The ``value`` is a ``JSONField`` so any TOML-shaped
scalar/list/dict round-trips (bool kill-switch, label string, int budget, list).

**Scope (per-overlay + global).** A row carries a ``scope``: the empty string
``""`` is the GLOBAL scope (applies to every overlay, the original #1775
behaviour), and a non-empty ``scope`` is an OVERLAY name (the same identifier
used in ``[overlays.<name>]``) that applies to that overlay alone. This mirrors
the TOML two-tier shape — a global ``[teatree]`` value and a per-overlay
``[overlays.<name>]`` override — in the DB: the resolver layers global DB rows
first, then the active overlay's DB rows on top, so an overlay-scoped row beats
a global DB row exactly as a per-overlay TOML override beats the global TOML
value. Uniqueness is the ``(scope, key)`` pair, so a global and an overlay row
for the same key coexist and the manager upserts within a scope.

**Seed provenance (#3435).** A row carries ``seeded_by`` (the seeder that owns
it — :data:`ENTRYPOINT_SEEDER` for a deploy seed, ``""`` for an operator/runtime
write) and ``seed_value`` (the exact value that seeder last wrote). Together they
let a redeploy tell a value the operator has pinned apart from one the deploy
seeded and the operator never touched: :meth:`ConfigSettingManager.seed` re-seeds
only a row it still owns (``seeded_by`` matches AND ``value == seed_value``),
never creates a row equal to the code default (which would only FREEZE a future
default change), and preserves any operator override. An explicit
:meth:`ConfigSettingManager.set_value` is an operator/runtime write, so it clears
the provenance — the row becomes operator-owned and no later deploy or doctor
autofix may touch it.

Bootstrap-readable settings (``DATABASE_URL`` / data-dir /
``DJANGO_SETTINGS_MODULE`` / the offline ``private_repos`` allowlist) are
explicitly out of scope — they must be readable before Django starts, so they can
never live here (#1775).
"""

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Sequence

# Any TOML/JSON-shaped value a setting may hold. Recursive in principle
# (lists/dicts nest), but the override registry only ever coerces scalars and
# flat lists, so the flat union is the honest, lint-clean alias (avoids ANN401's
# `Any`). ``None`` is NOT included — absence is the fall-through sentinel, and
# the pilot never stores a JSON null (see the manager docstring).
type ConfigValue = bool | int | float | str | list[object] | dict[str, object]

# The global scope sentinel: a ``ConfigSetting`` whose ``scope`` is the empty
# string applies to every overlay (the original #1775 single-tier behaviour). A
# non-empty ``scope`` is an overlay name that scopes the row to that overlay.
GLOBAL_SCOPE = ""

# The provenance marker :func:`deploy/entrypoint.sh`'s seed step stamps on a row
# it created, so a later redeploy re-seed and the ``t3 doctor --repair``
# concurrency autofix can tell a deploy-seeded row from an operator override.
ENTRYPOINT_SEEDER = "entrypoint"


def scope_label(scope: str) -> str:
    """Human label for a row's scope: ``global`` for the empty scope else ``overlay '<name>'``."""
    return "global" if not scope else f"overlay {scope!r}"


class SeedOutcome(StrEnum):
    """What :meth:`ConfigSettingManager.seed` did to the row (for operator logs)."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    PRESERVED = "preserved-operator-override"
    REMOVED = "removed-equals-default"
    SKIPPED_DEFAULT = "skipped-equals-default"


class ConfigSettingManager(models.Manager["ConfigSetting"]):
    """Read/write helpers for the DB override tier.

    The manager is the resolver's single entry point: it owns the
    absent-key → ``None`` fall-through contract and the upsert/clear admin
    operations, keeping the resolver (a different tach layer) free of any
    knowledge beyond "ask the manager". Every method takes a ``scope`` that
    defaults to :data:`GLOBAL_SCOPE` (``""``), so every existing global call
    site is byte-for-byte unchanged; a non-empty ``scope`` addresses an
    overlay-scoped row.
    """

    def get_effective(self, key: str, scope: str = GLOBAL_SCOPE) -> ConfigValue | None:
        """Return the stored value for *key* in *scope*, or ``None`` when no row exists.

        ``None`` is the fall-through sentinel: the resolver interprets it as
        "no DB override for this setting" and keeps the file/env value. An
        empty table therefore leaves every setting resolving exactly as it does
        today — the #1775 no-regression-during-migration invariant.
        """
        row = self.filter(scope=scope, key=key).first()
        return row.value if row is not None else None

    def reject_inconsistent_cross_key(self, key: str, value: ConfigValue, scope: str) -> None:
        """Raise :class:`ValidationError` when this write would land an inconsistent coupled pair (#3688).

        Delegates to the config-layer :func:`~teatree.config.cross_key_consistency.validate_cross_key_write`,
        resolving the paired key's current effective value from the DB tier
        (overlay-scope row, then global-scope row) so the RESULTING pair — not
        just the value in hand — is judged. A no-op for any key in no coupled
        pair. Imported lazily to keep the model module's cold-import cheap.

        Called by :meth:`set_value` (every programmatic write) and by
        :meth:`ConfigSetting.clean` (every ``ModelForm`` write, so the Django
        admin shows a field error instead of landing the bad pair).
        """
        from teatree.config.cross_key_consistency import (  # noqa: PLC0415 — deferred: heavy config import
            validate_cross_key_write,
        )

        def resolve_other(other_key: str) -> ConfigValue | None:
            stored = self.get_effective(other_key, scope=scope)
            if stored is None and scope != GLOBAL_SCOPE:
                stored = self.get_effective(other_key, GLOBAL_SCOPE)
            return stored

        if reason := validate_cross_key_write(key, value, resolve_other):
            raise ValidationError(reason)

    def set_value(self, key: str, value: ConfigValue, scope: str = GLOBAL_SCOPE) -> "ConfigSetting":
        """Upsert the override row for *key* in *scope* to *value* (admin path).

        The unique ``(scope, key)`` pair makes this an idempotent upsert:
        setting the same key in the same scope twice updates the one row rather
        than creating a duplicate. A global and an overlay-scoped row for the
        same key are distinct rows.

        An explicit ``set_value`` is an operator/runtime write, so it CLEARS any
        seed provenance (``seeded_by`` → ``""``, ``seed_value`` → ``None``): the
        row becomes operator-owned, and no later deploy re-seed or ``t3 doctor
        --repair`` autofix may overwrite or delete it (#3435 / #3434).

        Raises :class:`~django.core.exceptions.ValidationError` when the write
        would land an INCONSISTENT coupled-key pair (#3688) — e.g. an
        ``agent_harness_provider`` that no harness the resulting ``agent_harness``
        names would accept at dispatch. Rejecting at write time turns one bad
        config into one loud error, not a fleet-wide repair-halt flood on every
        later dispatch. The store is left untouched on rejection.
        """
        self.reject_inconsistent_cross_key(key, value, scope)
        row, _ = self.update_or_create(
            scope=scope,
            key=key,
            defaults={"value": value, "seeded_by": "", "seed_value": None},
        )
        return row

    def set_values(self, rows: "Sequence[tuple[str, ConfigValue, str]]") -> None:
        """Upsert every ``(key, value, scope)`` in one transaction, judged as one SET.

        A document moving a COUPLED pair (#3688) between two valid states has no
        safe row order — whichever half lands first leaves an invalid intermediate
        that :meth:`set_value` rejects, so a legitimate change was refused. Here the
        rows land first and consistency is asserted against the RESULT; the assert
        runs inside the transaction, so a genuinely invalid destination still raises
        :class:`~django.core.exceptions.ValidationError` and rolls every row back —
        an interrupted bulk write can never leave the store half-imported.
        """
        with transaction.atomic():
            for key, value, scope in rows:
                self.update_or_create(
                    scope=scope,
                    key=key,
                    defaults={"value": value, "seeded_by": "", "seed_value": None},
                )
            for key, value, scope in rows:
                self.reject_inconsistent_cross_key(key, value, scope)

    def seed(
        self,
        key: str,
        value: ConfigValue,
        *,
        code_default: object,
        seeded_by: str = ENTRYPOINT_SEEDER,
        scope: str = GLOBAL_SCOPE,
    ) -> SeedOutcome:
        """Provenance-aware deploy seed of *key* → *value* in *scope* (#3435).

        The idempotent policy a redeploy needs so a changed shipped default
        reaches existing boxes without ever clobbering an operator's pin:

        * **value == code_default** → never create a row (a code-default seed
            is a no-op that would only FREEZE a future default change). If a row
            this seeder still owns already holds that value, DELETE it so the
            live code default flows through again.
        * **no row** → create it, recording provenance (``seeded_by`` +
            ``seed_value = value``).
        * **row this seeder no longer owns** (a different ``seeded_by``, or
            ``value != seed_value`` because an operator edited it) → PRESERVE
            it untouched.
        * **row this seeder still owns** (``seeded_by`` matches AND
            ``value == seed_value``) → UPDATE it when the shipped seed changed,
            else no-op.

        *code_default* is the pure code default (the ``UserSettings`` field
        default with no env/DB layer). Pass a sentinel that never equals a real
        value for a non-``UserSettings`` key, so such a seed is always written.

        The still-owned decision is re-asserted in the ``WHERE`` of the write,
        not merely read before it: an operator ``set_value`` landing between the
        two clears the provenance, so the delete and the update match no row and
        the seed reports ``PRESERVED`` instead of overwriting a fresh pin.
        """
        row = self.filter(scope=scope, key=key).first()
        equals_default = value == code_default
        if row is None:
            if equals_default:
                return SeedOutcome.SKIPPED_DEFAULT
            self.create(scope=scope, key=key, value=value, seeded_by=seeded_by, seed_value=value)
            return SeedOutcome.CREATED
        if row.seeded_by != seeded_by or row.value != row.seed_value:
            return SeedOutcome.PRESERVED
        still_owned = self.filter(pk=row.pk, seeded_by=seeded_by, seed_value=row.seed_value, value=row.seed_value)
        if equals_default:
            deleted, _ = still_owned.delete()
            return SeedOutcome.REMOVED if deleted else SeedOutcome.PRESERVED
        if row.value == value:
            return SeedOutcome.UNCHANGED
        updated = still_owned.update(value=value, seed_value=value, seeded_by=seeded_by, updated_at=timezone.now())
        return SeedOutcome.UPDATED if updated else SeedOutcome.PRESERVED

    def clear(self, key: str, scope: str = GLOBAL_SCOPE) -> bool:
        """Delete the override row for *key* in *scope*; return whether one was removed.

        After ``clear`` the setting falls back through to the next tier
        (an overlay-scoped clear falls back to the global DB row / file / env;
        a global clear falls back to file / env). ``get_effective`` returns
        ``None`` again for that scope.
        """
        deleted, _ = self.filter(scope=scope, key=key).delete()
        return deleted > 0

    def overrides_for_scope(self, scope: str) -> dict[str, ConfigValue]:
        """Return ``{key: value}`` for every row in *scope* (one query).

        The resolver layers the global scope (``""``) then the active overlay's
        scope on top; this is the per-scope read it composes from, kept on the
        manager so the resolver never builds a ``ConfigSetting`` query itself.
        """
        return dict(self.filter(scope=scope).values_list("key", "value"))


class ConfigSetting(models.Model):
    """One DB-backed override of a ``UserSettings`` field, keyed by ``(scope, key)``.

    The ``key`` is the canonical ``UserSettings`` field name (e.g.
    ``issue_implementer_enabled``) — the same string used in
    ``OVERLAY_OVERRIDABLE_SETTINGS``. The ``scope`` is the empty string for the
    GLOBAL tier (every overlay) or an overlay name for an overlay-scoped
    override (the same identifier as ``[overlays.<name>]``). The ``value`` is
    stored as JSON so any TOML-shaped value round-trips. The ``(scope, key)``
    pair is unique so the manager's ``set_value`` is a clean per-scope upsert
    and a global + overlay row for one key can coexist.

    ``seeded_by`` / ``seed_value`` carry the seed provenance (#3435):
    ``seeded_by`` names the seeder that owns the row (:data:`ENTRYPOINT_SEEDER`
    for a deploy seed, ``""`` for an operator/runtime write) and ``seed_value``
    is the value that seeder last wrote. A redeploy re-seeds only a row it still
    owns, and the ``t3 doctor --repair`` concurrency autofix clears only an
    entrypoint-seeded pin — never an operator's deliberate one.
    """

    scope = models.CharField(max_length=255, default=GLOBAL_SCOPE, blank=True)
    key = models.CharField(max_length=255)
    value = models.JSONField(blank=True)
    seeded_by = models.CharField(max_length=255, default="", blank=True)
    seed_value = models.JSONField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects: ClassVar[ConfigSettingManager] = ConfigSettingManager()

    class Meta:
        db_table = "teatree_config_setting"
        ordering: ClassVar = ["scope", "key"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["scope", "key"], name="uniq_config_setting_scope_key"),
        ]

    def __str__(self) -> str:
        """Identify the row by coordinate ONLY — a value here would leak a stored secret.

        ``__str__`` reaches log lines, tracebacks, and the Django admin's object
        labels (the changelist action checkbox, the change-form breadcrumb), none of
        which consult the secret taxonomy. The coordinate is what identifies a row;
        reading its value is the settings surfaces' job, and they mask it.
        """
        where = "global" if self.scope == GLOBAL_SCOPE else f"overlay:{self.scope}"
        return f"config-setting<{where} {self.key}>"

    def clean(self) -> None:
        """Refuse a JSON ``null`` value and any inconsistent coupled pair (#3688).

        ``ModelForm`` validation runs this, so the Django admin refuses the same
        writes ``set_value`` refuses instead of landing them via ``Model.save()``.

        ``value`` is ``blank=True`` because ``[]`` / ``{}`` / ``""`` are all
        legitimate overrides (``statusline_chain = []`` means "override the
        shipped non-empty default with nothing"), and a generic key/value store
        cannot know any per-key arity — that belongs in the coercion layer.
        Django's required check keys on ``Field.empty_values``
        (``[None, "", [], (), {}]``), so ``blank=False`` is an all-or-nothing
        switch that rejects those legitimate values along with ``None``.

        ``blank=True`` alone is unsafe: an empty admin textarea cleans to
        ``None``, ``Model.clean_fields`` skips a blank-allowed empty value, and
        ``None`` reaches a NOT NULL column as a 500 rather than a form error.
        ``None`` is also the resolver's "no row, use the default" sentinel, so
        it is never a storable value — clear the row instead.
        """
        if self.value is None:
            raise ValidationError({"value": "Enter a JSON value — use [] or {} for an empty list or object."})
        ConfigSetting.objects.reject_inconsistent_cross_key(self.key, self.value, self.scope)
