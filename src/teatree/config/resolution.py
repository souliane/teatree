"""Effective-settings resolution — the DB-home partition + env + the autonomy collapse.

``get_effective_settings`` (the single resolver both the active-overlay and
named-overlay paths share), ``cadence_seconds``, and the autonomy-collapse
(``_apply_autonomy``). Split out of the package module for the module-health LOC
cap; re-exported from ``teatree.config``.

The #1775 partition: every ``UserSettings`` field has exactly one home (see
``config/homes.py``). The per-install file config tier was removed, so every field
is DB-home. A DB-home field's OVERRIDE tiers are the ``ConfigSetting`` store
(``_db_setting_overrides``:
global rows then the active overlay's rows on top) + ``T3_*`` env ONLY. A DB-home
key mistakenly placed in the DB overlays-registry entry (the ``[overlays.<name>]``
table in ``config.raw``) is NOT one of its homes, so it is dropped on read
(``drop_db_home_overlay_keys``). The DB read is fail-safe (an absent/empty table or
unconfigured Django yields no overrides) so an empty table resolves every DB-home
field to its shipped default.

Beneath every override sits the DEFAULTS base: the shipped, committed
``config/defaults.toml`` (``_toml_default_rows``) — packaged data, never per-install
config. It is read with stdlib ``tomllib`` through ``cold_defaults``, NEVER through
``schema.shipped_defaults``: ``teatree.config``'s package init imports this module
and the cold hook path imports that package, so a pydantic read here would put
~110ms on every hook invocation.
"""

import logging
import os
from dataclasses import replace
from typing import Any

import teatree.config as _facade
from teatree.config import cold_defaults
from teatree.config.discovery import _active_overlay_entry
from teatree.config.enums import Autonomy, Mode, OnBehalfPostMode
from teatree.config.overlay_code_defaults import overlay_code_defaults
from teatree.config.retired_settings import RENAMED_SETTING_KEYS, removed_setting, warn_removed_setting
from teatree.config.setting_layers import (
    SettingLayers,
    apply_structured_settings,
    drop_db_home_overlay_keys,
    shipped_defaults_base,
)
from teatree.config.setting_registries import ENV_SETTING_OVERRIDES, OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.settings import OverlayEntry, UserSettings
from teatree.request_cache import cached_per_request

_logger = logging.getLogger("teatree.config")

# The structured nested settings: stored as a JSON
# dict ConfigSetting, NOT a scalar. ``_coerce_setting_rows`` SKIPS them — a bare dict
# cannot flat-replace the dataclass field — and ``get_effective_settings`` resolves
# them bespoke from the raw row layers (``setting_layers.apply_structured_settings``):
# ``mr_reminder`` highest-layer-wins, ``speak`` as a MERGE up the layers.
_BESPOKE_STRUCTURED_FIELDS: frozenset[str] = frozenset({"speak", "mr_reminder"})

# Sentinel for "no shipped default at all" — never equals a real value, so a
# seed/import of such a key is always written.
_NO_EFFECTIVE_DEFAULT: object = object()


def _toml_default_rows() -> dict[str, Any]:
    """The shipped ``defaults.toml`` ``[teatree]`` table — the DEFAULTS base of every tier chain.

    Read through ``cold_defaults`` (stdlib ``tomllib``, mtime-cached), never through
    ``schema.shipped_defaults``: this module sits on ``teatree.config``'s package init,
    which the cold hook path imports, so a pydantic read here would cost ~110ms per hook
    invocation. The path is read off the module at call time so a test can point the tier
    at a fixture file.
    """
    return cold_defaults.shipped_defaults_table(cold_defaults.DEFAULTS_TOML)


def effective_default(key: str) -> object:
    """The value *key* resolves to with NO DB row / env override — the ONE default authority.

    The single source the seed-skip (``config_setting seed``), the import-skip
    (``config_migration``), and the resolver all agree on, so a row equal to it is
    provably redundant: writing it and clearing it resolve to the SAME value.

    A ``UserSettings`` scalar field resolves to its ``defaults.toml`` value — the
    resolver's own DEFAULTS base (``_toml_default_rows``, coerced by the same registry
    parsers a DB row goes through). A key the shipped file does not carry (a
    Secret/Personal key, absent by construction) falls back to the dataclass default.

    A structured field (``speak`` / ``mr_reminder``) is stored as a dict the resolver
    rebuilds bespoke; its stored-form default is the ``shipped_defaults`` dict (equal
    in meaning to the dataclass default), so it is compared in that stored form rather
    than against the dataclass instance. A non-``UserSettings`` key (cold / cold-hook
    / registry) resolves to its ``shipped_defaults`` value, which IS its resolver
    default (the cold reader / registry default sourced from ``defaults.toml``).

    Returns a never-equal sentinel for a key with no shipped default, so its
    seed/import is always written.
    """
    if key not in _BESPOKE_STRUCTURED_FIELDS:
        toml_default = _coerce_setting_rows(_toml_default_rows()).get(key, _NO_EFFECTIVE_DEFAULT)
        if toml_default is not _NO_EFFECTIVE_DEFAULT:
            return toml_default
        dataclass_default = getattr(UserSettings(), key, _NO_EFFECTIVE_DEFAULT)
        if dataclass_default is not _NO_EFFECTIVE_DEFAULT:
            return dataclass_default
    from teatree.config.schema import shipped_defaults  # noqa: PLC0415 — deferred: heavy pydantic import

    return getattr(shipped_defaults(), key, _NO_EFFECTIVE_DEFAULT)


@cached_per_request
def get_effective_settings(overlay_name: str | None = None) -> UserSettings:
    """Return the user settings under the #1775 DB-home partition + env.

    Every ``UserSettings`` field has exactly ONE home (see ``config/homes.py``).
    The per-install file config tier was removed, so every field is DB-home. A
    DB-home field resolves, first match wins:

        env -> DB(overlay scope) -> DB(global scope) -> overlay code default -> TOML default.

    ``T3_*`` env var, then the ``ConfigSetting`` store (overlay-scope row, then
    global-scope row), then — for a key promoted to an overlay code default (#36,
    ``overlay_code_defaults``) — the active overlay's ``OverlayConfig`` value, then
    the shipped ``defaults.toml`` value (:func:`_toml_default_rows`, coerced through
    the same registry parsers a stored row goes through). A field the shipped file
    does not carry — a Secret/Personal key, absent by construction — keeps its
    dataclass default, which is the resolver base. A value for the field in the DB
    overlays-registry entry (its ``[overlays.<name>]`` table in ``config.raw``) is
    NOT one of its homes and is dropped on read. Both default tiers are DEFAULTS
    (never hard pins), so they sit below every DB / env override and must not defeat
    the autonomy collapse.

    The per-overlay overlays-registry override layer is filtered by home
    (``setting_layers.drop_db_home_overlay_keys`` / ``toml_home``) so a ``[overlays.<name>]``
    value for a DB-home key never leaks in: every such key is dropped with a loud
    WARN. That home filter governs a field's OVERRIDE tier and is orthogonal to the
    TOML default tier, which is a shipped base under every field. The DB read fails
    safe to ``{}`` whenever Django is not
    configured or the table does not exist yet, so an empty table resolves every
    DB-home field to its shipped default.

    The DB tier has TWO scopes: a GLOBAL ``ConfigSetting`` row (``scope=""``)
    applies to every overlay, and an OVERLAY-scoped row (``scope=<overlay name>``)
    applies to that overlay alone. The resolver layers global rows first, then the
    active overlay's rows on top — so an overlay-scoped DB row beats a global DB
    row.

    The active overlay is resolved via ``T3_OVERLAY_NAME`` first (matches
    ``get_overlay()``), then cwd-based discovery, then the single
    installed overlay.

    ``overlay_name`` resolves a SPECIFIC named overlay instead of the active
    one — the loop's scanner-builders fan out over every registered overlay,
    not just the session's. In that mode the env layer is NOT applied; the DB
    tier, the per-overlay ``[overlays.<name>]`` overrides, and the autonomy
    collapse run identically. This is the single resolver both paths share.

    To make an additional setting DB-overridable, add it to
    ``OVERLAY_OVERRIDABLE_SETTINGS`` (the DB-home registry) or
    ``ENV_SETTING_OVERRIDES`` (env); the resolver picks it up generically via
    ``dataclasses.replace``. The two non-generic fields are the nested structured
    tables ``speak`` / ``mr_reminder`` (``_BESPOKE_STRUCTURED_FIELDS``): they are
    stored as JSON dicts, so ``_coerce_setting_rows`` skips them and
    ``setting_layers.apply_structured_settings`` rebuilds the dataclass from the raw row
    layers (TOML default, DB global, DB overlay) — ``mr_reminder`` highest-layer-wins,
    ``speak`` as a MERGE up the layers (a partial row overrides only the keys it sets).

    As a final step the single ``autonomy`` switch is applied: under
    :attr:`Autonomy.FULL` / :attr:`Autonomy.NOTIFY` the three approval gates
    collapse to their autonomous value and ``mode`` is pinned to ``auto``
    (unless the user pinned a gate explicitly). See :func:`_apply_autonomy`.
    """
    config = _facade.load_config()
    base = config.user
    if overlay_name is not None:
        overrides = _overlay_overrides_by_name(overlay_name)
    else:
        active = _active_overlay_entry()
        overrides = dict(active.overrides) if active is not None else {}
    # The #1775 partition: every ``[overlays.<name>]`` value for a DB-home key is dropped
    # on read (that field's authoritative override tier is the DB store below). The drop
    # is LOUD (never silent) so an operator who set a DB-home key in their
    # overlays-registry entry is told the value had no effect.
    overrides = drop_db_home_overlay_keys(overrides, _resolved_overlay_name(overlay_name))
    # ``hard_pinned`` (a per-overlay/env opinion that beats the autonomy collapse,
    # including for ``mode``) is the per-overlay override layer so far. DB-home fields
    # get their SOLE value from ``ConfigSetting``: the GLOBAL scope is a workspace
    # default (NOT a hard pin), the OVERLAY scope is a per-overlay opinion (a hard
    # pin), env beats both.
    resolved_overlay = _resolved_overlay_name(overlay_name)
    layers = read_setting_layers(resolved_overlay)
    # The overlay-code-default tier (#36): promoted constants the active overlay
    # supplies, layered BELOW every DB / env override (a row overrides) and ABOVE the
    # shipped TOML default (with no row the code default wins). Neither default tier is
    # a hard pin, so neither may defeat the autonomy collapse.
    code_defaults = overlay_code_defaults(resolved_overlay)
    hard_pinned = set(overrides) | set(layers.overlay_db)
    overrides.update(layers.global_db)
    overrides.update(layers.overlay_db)
    if overlay_name is None:
        env_overrides = env_setting_overrides()
        overrides.update(env_overrides)
        hard_pinned |= set(env_overrides)
    defaults_base = shipped_defaults_base(base, layers)
    layered = {**code_defaults, **overrides}
    settings = defaults_base if not layered else replace(defaults_base, **layered)
    settings = apply_structured_settings(settings, layers.db_rows, defaults_base.speak)
    # ``global_pinned`` MUST be the FOLDED field names (``layers.global_db``), not the raw
    # row keys: a global row stored under a retired alias (``_LEGACY_SETTING_ALIASES``)
    # resolves its VALUE onto the current field via ``_coerce_setting_rows``, so its pin
    # must be recorded under that same current field name. Keying the pin set off the raw
    # row keys would let a renamed approval-gate field's value resolve while its pin
    # silently vanished — the autonomy collapse would then override an explicitly-stored
    # gate (config §3d #1).
    return _apply_autonomy(
        settings,
        hard_pinned=hard_pinned,
        global_pinned=set(layers.global_db),
    )


def read_setting_layers(overlay_name: str) -> SettingLayers:
    """Read the shipped-defaults table and both ``ConfigSetting`` scopes, coerced once.

    Public because it is the ONE place the persisted tiers are read: the resolver folds
    them into a ``UserSettings``, and ``config.provenance`` walks the same tiers to say
    WHICH one supplied a value. A second reader would be a second resolution path.
    """
    toml_rows = _toml_default_rows()
    db_rows = (_load_global_rows(), _load_overlay_rows(overlay_name))
    global_db, overlay_db = (_coerce_setting_rows(rows) for rows in db_rows)
    return SettingLayers(toml_rows, _coerce_setting_rows(toml_rows), db_rows, global_db, overlay_db)


def _active_overlay_overrides() -> dict[str, Any]:
    """Per-overlay overrides for the active overlay, with the DB + env layers applied.

    Precedence (later wins): per-overlay overlays-registry override -> DB tier ->
    env. Retained as the composed helper for the public re-export;
    :func:`get_effective_settings` layers the same tiers inline so the
    named-overlay path can skip the env layer.
    """
    active = _active_overlay_entry()
    overrides: dict[str, Any] = dict(active.overrides) if active is not None else {}
    overrides = drop_db_home_overlay_keys(overrides, _resolved_overlay_name(None))
    overrides.update(_db_setting_overrides(_resolved_overlay_name(None)))
    overrides.update(env_setting_overrides())
    return overrides


def env_setting_overrides() -> dict[str, Any]:
    """``T3_*`` env overrides, the highest-precedence tier (see ``ENV_SETTING_OVERRIDES``).

    Public for the same reason as :func:`read_setting_layers`: provenance names this tier.
    """
    overrides: dict[str, Any] = {}
    for env_var, (field_name, parser) in ENV_SETTING_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is not None:
            overrides[field_name] = parser(raw)
    return overrides


def _resolved_overlay_name(overlay_name: str | None) -> str:
    """The overlay name whose per-overlay DB rows the resolver should layer.

    For the named-overlay path this is the explicit ``overlay_name``; for the
    active-overlay path it is ``T3_OVERLAY_NAME`` if set, then the cwd/single
    discovered overlay — the same active-overlay resolution the per-overlay
    overlays-registry layer uses, so the DB scope and the overlays-registry layer
    always agree on which overlay is active. ``""`` (no resolvable overlay) means
    only the global DB scope applies.
    """
    if overlay_name is not None:
        return overlay_name
    env_name = os.environ.get("T3_OVERLAY_NAME")
    if env_name:
        return env_name
    active = _active_overlay_entry()
    return active.name if active is not None else ""


def _db_setting_overrides(overlay_name: str = "") -> dict[str, Any]:
    """The ``ConfigSetting`` DB-home tier (#1775) — global then per-overlay, layered.

    The composed reader (global then *overlay_name* on top, later wins). Kept for
    callers that want the merged value without distinguishing the pin scope;
    :func:`get_effective_settings` instead reads the two scopes separately (so a
    global-scope ``mode`` is a workspace default while an overlay-scope ``mode``
    is a hard pin). See :func:`_db_global_overrides` / :func:`_db_overlay_overrides`.
    """
    return {**_db_global_overrides(), **_db_overlay_overrides(overlay_name)}


def _db_global_overrides() -> dict[str, Any]:
    """Coerced ``{field: value}`` for every GLOBAL-scope (``scope=""``) DB-home row.

    The DB twin of the global ``[teatree]`` table: applies to every overlay. A
    global ``mode`` row is a workspace default that does NOT pin ``mode`` against
    the autonomy collapse (mirroring the old global-``[teatree] mode`` rule). See
    :func:`_coerce_setting_rows` for the type coercion and the loud-on-corruption rule.
    """
    return _coerce_setting_rows(_load_global_rows())


def _db_overlay_overrides(overlay_name: str = "") -> dict[str, Any]:
    """Coerced ``{field: value}`` for the active overlay's DB-home rows.

    The DB twin of a per-overlay ``[overlays.<name>]`` override: a deliberate
    per-overlay opinion that beats the global DB row AND the autonomy collapse
    (it is a hard pin). The overlay scope is matched canonical-alias-tolerantly (a
    request for ``teatree`` also reads the ``t3-teatree`` entry-point overlay's
    rows and vice versa) so a row written under either spelling resolves.
    """
    return _coerce_setting_rows(_load_overlay_rows(overlay_name))


# Retired ConfigSetting keys mapped to their current ``UserSettings`` field, and
# the retired keys with no replacement. Both are DERIVED from the one registry in
# ``config.retired_settings`` (#3527) so a retirement is recorded exactly once: a
# renamed key's stored row resolves onto the replacement field (the canonical key
# still wins when both rows exist), and a removed key's stored row is reported
# loudly rather than dropped in silence.
_LEGACY_SETTING_ALIASES: dict[str, str] = RENAMED_SETTING_KEYS
_RETIRED_SETTING_KEYS: frozenset[str] = frozenset(RENAMED_SETTING_KEYS)


def _coerce_setting_rows(rows: dict[str, Any]) -> dict[str, Any]:
    """Coerce a ``{key: stored value}`` table via the DB-home parser registry.

    Shared by every tier that arrives in stored form: the ``ConfigSetting`` rows of
    both scopes AND the shipped ``defaults.toml`` table — ``defaults.toml`` is written
    in exactly the ``config_setting export``/``import`` shape, so one coercer keeps the
    defaults tier and the override tiers provably type-identical.

    Returns ``{field: coerced}`` for every key that is a registered
    ``OVERLAY_OVERRIDABLE_SETTINGS`` (= DB-home) field; unknown / non-DB keys are
    dropped so neither a stray row nor a cold-hook-only ``defaults.toml`` key ever
    mutates the resolved settings. A key written under a retired name
    (``_LEGACY_SETTING_ALIASES``) is folded onto its current field name; the canonical
    key wins when both are present.

    A row under a REMOVED key (``retired_settings.REMOVED_SETTING_KEYS``) has no
    field to resolve onto, so it is reported on stderr naming the key, the reason
    and the remedy before falling through to the default (#3527) — loud rather
    than fatal, so a stale row never locks an operator out of their own factory.

    A per-row parser failure means a stored value is invalid for its setting's
    type (an out-of-enum ``mode``, a quoted ``"false"`` for a bool). Write-time
    validation (``config_setting set``, #258) means such a row can only exist via
    out-of-band corruption — so it is raised LOUD with the offending key named,
    never swallowed back to the default with no signal.
    """
    overrides: dict[str, Any] = {}
    fields_from_canonical_key: set[str] = set()
    for key, value in rows.items():
        removed = removed_setting(key)
        if removed is not None:
            warn_removed_setting(removed)
            continue
        is_alias = key in _LEGACY_SETTING_ALIASES
        field_name = _LEGACY_SETTING_ALIASES.get(key, key)
        if field_name in _BESPOKE_STRUCTURED_FIELDS:
            continue  # resolved bespoke in get_effective_settings (dict -> dataclass + merge)
        parser = OVERLAY_OVERRIDABLE_SETTINGS.get(field_name)
        if parser is None:
            continue
        # The canonical key is authoritative; a legacy-alias row only fills a gap
        # and never overwrites a value the current key already supplied — order-
        # independent, so it holds regardless of which row is iterated first.
        if is_alias and field_name in fields_from_canonical_key:
            continue
        try:
            coerced = parser(value)
        except (ValueError, TypeError, AttributeError) as exc:
            msg = f"Invalid stored ConfigSetting value for {key!r}: {exc}"
            raise ValueError(msg) from exc
        overrides[field_name] = coerced
        if not is_alias:
            fields_from_canonical_key.add(field_name)
    return overrides


def _app_registry_ready() -> bool:
    """True when Django is configured AND its app registry is fully populated (post-``django.setup()``)."""
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time
    from django.conf import settings as django_settings  # noqa: PLC0415 — deferred: settings read at call time

    return django_settings.configured and apps.ready


def _override_read_degrades_silently(exc: BaseException) -> bool:
    """Whether a caught override-read exception is a genuine BOOTSTRAP no-op (silent ``{}``).

    ``ImproperlyConfigured`` / ``AppRegistryNotReady`` are unambiguous bootstrap states —
    always silent. ``OperationalError`` / ``ProgrammingError`` are AMBIGUOUS: a bootstrap
    signal (missing table, DB not ready) before ``django.setup()``, but ALSO a real RUNTIME
    fault (a locked SQLite DB, a lock timeout, a mid-session drop) once the registry is
    ready — the TYPE alone can't tell them apart, so they are silent ONLY while the registry
    is not ready (:func:`_app_registry_ready`); a runtime one logs loud. Any OTHER exception
    is a real read bug — always loud.
    """
    from django.core.exceptions import (  # noqa: PLC0415 — deferred: Django import at call time
        AppRegistryNotReady,
        ImproperlyConfigured,
    )
    from django.db.utils import (  # noqa: PLC0415 — deferred: Django import at call time
        OperationalError,
        ProgrammingError,
    )

    if isinstance(exc, ImproperlyConfigured | AppRegistryNotReady):
        return True
    if isinstance(exc, OperationalError | ProgrammingError):
        return not _app_registry_ready()
    return False


# The loud SIGNAL for a non-bootstrap ``ConfigSetting`` read fault. Such a failure is a
# fail-OPEN of the ENTIRE DB override tier — it drops the ``autonomy`` /
# ``require_human_approval_to_merge`` safety gates back to the dataclass defaults — so it
# is logged ``ERROR`` + traceback (the "raise or log-and-signal, not SILENTLY fail-open"
# contract) rather than swallowed: operator error-monitoring surfaces the real fault.
_OVERRIDE_READ_FAILURE_MSG = (
    "ConfigSetting %s-scope override read FAILED unexpectedly — resolving with NO DB override tier for this "
    "read (safety gates fall back to dataclass defaults). This is a real read fault, not a bootstrap no-op; "
    "fix the DB/read error."
)


def _load_global_rows() -> dict[str, Any]:
    """Read the GLOBAL-scope (``scope=""``) ``{key: value}`` rows, or ``{}`` on failure.

    Reaches the model via Django's app registry (no static ``teatree.core`` import — that
    would be a backwards ``platform -> domain`` tach edge). A genuine bootstrap state
    degrades SILENTLY (:func:`_override_read_degrades_silently`); a RUNTIME fault — incl.
    an ``OperationalError`` / ``ProgrammingError`` raised while the app registry is ready —
    is logged loud (:data:`_OVERRIDE_READ_FAILURE_MSG`), never silently emptying the tier.
    """
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

    try:
        model = apps.get_model("core", "ConfigSetting")
        return dict(model.objects.overrides_for_scope(""))
    except Exception as exc:
        if not _override_read_degrades_silently(exc):
            _logger.exception(_OVERRIDE_READ_FAILURE_MSG, "global")
        return {}


def _load_overlay_rows(overlay_name: str = "") -> dict[str, Any]:
    """Read the active overlay's ``{key: value}`` rows, alias-tolerant, or ``{}``.

    Matches the row's scope to *overlay_name* canonical-alias-tolerantly (a row
    under either the short alias or the ``t3-``-prefixed entry-point name resolves
    for the active overlay) and MERGES every canonically-equivalent scope group —
    a row scoped ``myovl`` and one scoped ``t3-myovl`` both apply. Alias groups
    apply in sorted-scope order, then the exact-name group last, so on a key
    collision the exact-name row wins. Same signal-on-real-failure posture as
    :func:`_load_global_rows`: a genuine bootstrap state is silent, a runtime fault
    (incl. a ready-registry ``OperationalError`` / ``ProgrammingError``) logs loud.
    """
    if not overlay_name:
        return {}
    from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

    try:
        model = apps.get_model("core", "ConfigSetting")
        canonical = OverlayEntry.canonical_overlay_name(overlay_name)
        scope_values: dict[str, dict[str, Any]] = {}
        for scope, key, value in model.objects.exclude(scope="").values_list("scope", "key", "value"):
            if scope == overlay_name or OverlayEntry.canonical_overlay_name(scope) == canonical:
                scope_values.setdefault(scope, {})[key] = value
        merged: dict[str, Any] = {}
        for scope in sorted(scope_values):
            if scope != overlay_name:
                merged.update(scope_values[scope])
        merged.update(scope_values.get(overlay_name, {}))
    except Exception as exc:
        if not _override_read_degrades_silently(exc):
            _logger.exception(_OVERRIDE_READ_FAILURE_MSG, f"overlay {overlay_name!r}")
        return {}
    return merged


def _overlay_overrides_by_name(overlay_name: str) -> dict[str, Any]:
    """Per-overlay overrides for a NAMED overlay (no env layer — see caller).

    The match is canonical-alias-tolerant: a request for the short alias
    ``teatree`` resolves the ``t3-``-prefixed entry-point overlay's
    ``[overlays.t3-teatree]`` overrides, and vice versa. ``ticket.overlay``
    and ``infer_overlay_for_url`` return the entry-point name while older
    rows / configs may carry the bare alias; an exact-name-only match would
    silently drop the per-overlay values (and an autonomous overlay would
    resolve to ``babysit``).
    """
    canonical = OverlayEntry.canonical_overlay_name(overlay_name)
    for entry in _facade.discover_overlays():
        if not entry.overrides:
            continue
        if entry.name == overlay_name or OverlayEntry.canonical_overlay_name(entry.name) == canonical:
            return dict(entry.overrides)
    return {}


#: The approval gates an autonomous tier collapses. ``require_human_approval_to_merge``
#: is deliberately absent (#3630): "carry the work end to end" and "merge without a
#: review gate" are different decisions, and a tier switch that silently made the second
#: one for the operator removed review-before-merge with no signal. Merging without
#: review is now its own named opt-in — an explicit
#: ``require_human_approval_to_merge = false`` — which every tier reads unchanged.
_AUTONOMY_COLLAPSED_GATE_VALUES: dict[str, Any] = {
    "on_behalf_post_mode": OnBehalfPostMode.IMMEDIATE,
    "require_human_approval_to_answer": False,
}


_AUTONOMOUS_TIERS: frozenset[Autonomy] = frozenset({Autonomy.NOTIFY, Autonomy.FULL})


def _apply_autonomy(settings: UserSettings, *, hard_pinned: set[str], global_pinned: set[str]) -> UserSettings:
    """Collapse the tier-governed approval gates for ``full`` / ``notify``.

    The set is :data:`_AUTONOMY_COLLAPSED_GATE_VALUES`, which excludes
    ``require_human_approval_to_merge`` (#3630) — no tier removes review before merge.

    Both autonomous tiers fill only the gates the user left unpinned and pin
    ``mode`` to ``auto`` (the merge-autonomy path is gated on ``mode == AUTO``,
    so a ``full``/``notify`` overlay that forgot ``mode`` would otherwise be a
    silent no-op). The ``notify`` tier additionally derives
    ``notify_on_behalf = True`` so every on-behalf action DMs the user.
    Both tiers also set the resolved ``review_request_post_disabled`` off the tier
    (#2579, replacing the deleted ``agent_review_request_disabled`` side flag):
    ``notify`` → ``True`` (collaborative/customer surface BLOCKs review-request),
    ``full`` → ``False`` (solo tooling surface PROCEEDs). ``babysit`` is a no-op —
    every gate keeps its resolved value, so review-request follows
    ``on_behalf_post_mode`` like any other colleague-visible post.

    Pin precedence:

    *   For the three approval gates, an explicit pin of EITHER kind
        (``hard_pinned`` = env / per-overlay override, or ``global_pinned`` =
        a global ``[teatree]`` key) wins — a deliberate opinion is never
        silently overridden.
    *   For ``mode`` only, a global ``[teatree] mode`` does NOT win (it is a
        workspace default, not an opinion about this overlay); only a
        ``hard_pinned`` per-overlay/env ``mode`` keeps the user's value. This
        is the over-pin fix: a common global ``mode = "interactive"`` no longer
        leaves an autonomous overlay half-collapsed.

    The safety floor is untouched: only the keys in
    :data:`_AUTONOMY_COLLAPSED_GATE_VALUES` (plus ``mode`` and the derived
    ``notify_on_behalf``) are ever written here.
    """
    if settings.autonomy not in _AUTONOMOUS_TIERS:
        return settings
    gate_pinned = hard_pinned | global_pinned
    relaxed: dict[str, Any] = {
        field_name: value
        for field_name, value in _AUTONOMY_COLLAPSED_GATE_VALUES.items()
        if field_name not in gate_pinned
    }
    if "mode" not in hard_pinned:
        relaxed["mode"] = Mode.AUTO
    if settings.autonomy is Autonomy.NOTIFY and "notify_on_behalf" not in gate_pinned:
        relaxed["notify_on_behalf"] = True
    # Review-request blocking is driven off the tier (#2579), replacing the
    # deleted ``agent_review_request_disabled`` side flag. The ``notify`` tier
    # (collaborative/customer surface) BLOCKs review-request; ``full`` (solo
    # tooling surface) PROCEEDs. An explicit per-overlay pin always wins (Option
    # A — the per-overlay escape), so the field is only set for the tier when the
    # user has not pinned it themselves.
    if "review_request_post_disabled" not in gate_pinned:
        relaxed["review_request_post_disabled"] = settings.autonomy is Autonomy.NOTIFY
    if not relaxed:
        return settings
    return replace(settings, **relaxed)


def cadence_seconds() -> int:
    """Resolve the loop slot cadence in seconds (minimum 60s).

    This setting is not registered in ``ENV_SETTING_OVERRIDES`` — its env
    layer is a bespoke direct read, so its resolution does NOT go through
    the generic effective-settings env layer. Layers, first match wins:
    first the ``T3_LOOP_CADENCE`` env var (the bespoke direct read), then
    ``get_effective_settings().loop_cadence_seconds`` which covers the
    per-overlay ``ConfigSetting`` overlay-scope row, then the global-scope
    row, then the ``UserSettings`` default of 720.

    Any ``T3_LOOP_CADENCE`` parse failure falls back to 720. The result is
    clamped to a 60s minimum so a misconfigured tiny value cannot busy-loop
    the tick.
    """
    raw = os.environ.get("T3_LOOP_CADENCE")
    if raw is not None and raw.strip():
        try:
            return max(60, int(raw.strip()))
        except ValueError:
            return 720
    return max(60, get_effective_settings().loop_cadence_seconds)


def worker_is_quiescing() -> bool:
    """True when the worker is draining for a deploy — admit NO new claims (read at the claim chokepoint only)."""
    return get_effective_settings().worker_quiescing
