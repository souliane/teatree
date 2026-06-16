"""Effective-settings resolution — the DB/TOML hard partition + env + the autonomy collapse.

``get_effective_settings`` (the single resolver both the active-overlay and
named-overlay paths share), ``cadence_seconds``, and the autonomy-collapse
(``_apply_autonomy``). Split out of the package module for the module-health LOC
cap; re-exported from ``teatree.config``.

The #1775 hard partition: every ``UserSettings`` field has exactly one home (see
``config/homes.py``). A DB-home field resolves from the ``ConfigSetting`` store
(``_db_setting_overrides``: global rows then the active overlay's rows on top) +
``T3_*`` env ONLY — its ``[teatree]`` / ``[overlays.<name>]`` TOML tables are not
read. A TOML-home field resolves from ``[teatree]`` / ``[overlays.<name>]`` (the
per-overlay layer, filtered to TOML-home keys) + env ONLY — a ``ConfigSetting``
row for it is ignored. The DB read is fail-safe (an absent/empty table or
unconfigured Django yields no overrides) so an empty table resolves every DB-home
field to its dataclass default.
"""

import os
from dataclasses import is_dataclass, replace
from typing import Any

import teatree.config as _facade
from teatree.config.discovery import _active_overlay_entry
from teatree.config.enums import Autonomy, Mode, OnBehalfPostMode
from teatree.config.homes import SETTING_HOMES, SettingHome
from teatree.config.settings import (
    ENV_SETTING_OVERRIDES,
    OVERLAY_OVERRIDABLE_SETTINGS,
    OverlayEntry,
    TeaTreeConfig,
    UserSettings,
)
from teatree.config_speak import speak_from_subtable
from teatree.types import SpeakConfig


def get_effective_settings(overlay_name: str | None = None) -> UserSettings:
    """Return the user settings under the #1775 DB/TOML hard partition + env.

    Every ``UserSettings`` field has exactly ONE home (see ``config/homes.py``),
    so resolution per field depends on that home (first match wins):

    *   DB-home field — ``T3_*`` env var, then the ``ConfigSetting`` store
        (overlay-scope row, then global-scope row), then the dataclass default:

            env -> DB(overlay scope) -> DB(global scope) -> default.

        Its ``[teatree]`` / ``[overlays.<name>]`` TOML value is NOT read.
    *   TOML-home field — ``T3_*`` env var, then the active overlay's
        ``[overlays.<name>]`` override, then the global ``[teatree]`` value, then
        the dataclass default:

            env -> per-overlay TOML -> global [teatree] -> default.

        A ``ConfigSetting`` row for it is ignored on read.

    The per-overlay TOML layer is filtered to TOML-home keys (``_toml_home``) so a
    ``[overlays.<name>]`` value for a DB-home key never leaks in. The DB read fails
    safe to ``{}`` whenever Django is not configured or the table does not exist
    yet, so an empty table resolves every DB-home field to its dataclass default.

    The DB tier has TWO scopes, mirroring the TOML two-tier shape: a GLOBAL
    ``ConfigSetting`` row (``scope=""``) applies to every overlay, and an
    OVERLAY-scoped row (``scope=<overlay name>``) applies to that overlay alone.
    The resolver layers global rows first, then the active overlay's rows on top —
    so an overlay-scoped DB row beats a global DB row, exactly as a per-overlay
    ``[overlays.<name>]`` TOML value beats the global ``[teatree]`` value.

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
    ``dataclasses.replace``. The one non-generic override is ``speak`` (a
    TOML-home field): its ``[overlays.<name>.speak]`` sub-table MERGES onto the
    base (see :func:`_overlay_speak_override`) rather than flat-replacing, so a
    partial table overrides only the keys it sets.

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
    # The hard partition (#1775): the per-overlay TOML layer applies ONLY to
    # TOML-home keys. A ``[overlays.<name>]`` value for a DB-home key is ignored
    # on read — that field's authoritative tier is the DB store below.
    overrides = {k: v for k, v in overrides.items() if _toml_home(k)}
    # ``hard_pinned`` (a per-overlay/env opinion that beats the autonomy collapse,
    # including for ``mode``) is the per-overlay TOML layer so far. DB-home fields
    # get their SOLE value from ``ConfigSetting``: the GLOBAL scope is a workspace
    # default (NOT a hard pin), the OVERLAY scope is a per-overlay opinion (a hard
    # pin), env beats both.
    resolved_overlay = _resolved_overlay_name(overlay_name)
    global_db = _db_global_overrides()
    overlay_db = _db_overlay_overrides(resolved_overlay)
    hard_pinned = set(overrides) | set(overlay_db)
    overrides.update(global_db)
    overrides.update(overlay_db)
    if overlay_name is None:
        env_overrides = _env_setting_overrides()
        overrides.update(env_overrides)
        hard_pinned |= set(env_overrides)
    settings = base if not overrides else replace(base, **overrides)
    speak_override = _overlay_speak_override(config, overlay_name, base.speak)
    if speak_override is not None:
        settings = replace(settings, speak=speak_override)
    settings = _apply_autonomy(
        settings,
        hard_pinned=hard_pinned,
        global_pinned=_global_pinned_fields(config),
    )
    # ``ask_before_post_on_behalf`` is DERIVED (#1775) from the resolved DB-home
    # ``on_behalf_post_mode``: True unless the mode is IMMEDIATE. Re-derived here
    # so the deprecated legacy field stays consistent with the effective mode.
    # Guarded on a real dataclass so a test that patches ``load_config`` to return
    # a bare mock (``config.user`` is not a ``UserSettings``) gets the same
    # untouched passthrough it got before the partition — the derivation is only
    # meaningful on a real settings dataclass anyway.
    if not is_dataclass(settings):
        return settings
    return replace(
        settings,
        ask_before_post_on_behalf=settings.on_behalf_post_mode is not OnBehalfPostMode.IMMEDIATE,
    )


def _overlay_speak_override(
    config: "TeaTreeConfig",
    overlay_name: str | None,
    base: SpeakConfig,
) -> SpeakConfig | None:
    """Merge a per-overlay ``[overlays.<name>.speak]`` sub-table onto ``base`` (#2050).

    The single non-generic override (see :func:`get_effective_settings`):
    merges only the keys the overlay table sets. ``None`` → base stands.
    """
    name = overlay_name if overlay_name is not None else os.environ.get("T3_OVERLAY_NAME", "")
    if not name:
        return None
    overlays = config.raw.get("overlays") or {}
    canonical = OverlayEntry.canonical_overlay_name(name)
    for table_name, overlay_cfg in overlays.items():
        if not isinstance(overlay_cfg, dict):
            continue
        if table_name != name and OverlayEntry.canonical_overlay_name(table_name) != canonical:
            continue
        subtable = overlay_cfg.get("speak")
        if isinstance(subtable, dict):
            return speak_from_subtable(subtable, base=base)
    return None


def _active_overlay_overrides() -> dict[str, Any]:
    """Per-overlay overrides for the active overlay, with the DB + env layers applied.

    Precedence (later wins): per-overlay TOML -> DB tier -> env. Retained as the
    composed helper for the public re-export; :func:`get_effective_settings`
    layers the same tiers inline so the named-overlay path can skip the env layer.
    """
    active = _active_overlay_entry()
    overrides: dict[str, Any] = dict(active.overrides) if active is not None else {}
    overrides = {k: v for k, v in overrides.items() if _toml_home(k)}
    overrides.update(_db_setting_overrides(_resolved_overlay_name(None)))
    overrides.update(_env_setting_overrides())
    return overrides


def _env_setting_overrides() -> dict[str, Any]:
    """``T3_*`` env overrides, the highest-precedence tier (see ``ENV_SETTING_OVERRIDES``)."""
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
    discovered overlay — the same active-overlay resolution the per-overlay TOML
    layer uses, so the DB and TOML overlay tiers always agree on which overlay
    is active. ``""`` (no resolvable overlay) means only the global DB scope
    applies.
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
    :func:`_coerce_db_rows` for the type coercion and the loud-on-corruption rule.
    """
    return _coerce_db_rows(_load_global_rows())


def _db_overlay_overrides(overlay_name: str = "") -> dict[str, Any]:
    """Coerced ``{field: value}`` for the active overlay's DB-home rows.

    The DB twin of a per-overlay ``[overlays.<name>]`` override: a deliberate
    per-overlay opinion that beats the global DB row AND the autonomy collapse
    (it is a hard pin). The overlay scope is matched canonical-alias-tolerantly (a
    request for ``teatree`` also reads the ``t3-teatree`` entry-point overlay's
    rows and vice versa) so a row written under either spelling resolves.
    """
    return _coerce_db_rows(_load_overlay_rows(overlay_name))


def _coerce_db_rows(rows: dict[str, Any]) -> dict[str, Any]:
    """Coerce stored ``ConfigSetting`` values via the DB-home parser registry.

    Returns ``{field: coerced}`` for every row whose key is a registered
    ``OVERLAY_OVERRIDABLE_SETTINGS`` (= DB-home) field; rows for unknown / non-DB
    keys are dropped so a stray row never mutates the resolved settings.

    A per-row parser failure means a stored value is invalid for its setting's
    type (an out-of-enum ``mode``, a quoted ``"false"`` for a bool). Write-time
    validation (``config_setting set``, #258) means such a row can only exist via
    out-of-band corruption — so it is raised LOUD with the offending key named,
    never swallowed back to the default with no signal.
    """
    overrides: dict[str, Any] = {}
    for key, value in rows.items():
        parser = OVERLAY_OVERRIDABLE_SETTINGS.get(key)
        if parser is None:
            continue
        try:
            overrides[key] = parser(value)
        except (ValueError, TypeError, AttributeError) as exc:
            msg = f"Invalid stored ConfigSetting value for {key!r}: {exc}"
            raise ValueError(msg) from exc
    return overrides


def _load_global_rows() -> dict[str, Any]:
    """Read the GLOBAL-scope (``scope=""``) ``{key: value}`` rows, or ``{}``.

    Reaches the model via Django's app registry (no static ``teatree.core``
    import — that would be a backwards ``platform -> domain`` tach edge). Fails
    safe to ``{}`` for any early/unconfigured read (apps not ready, no settings,
    pre-migration table, DB unreachable) so the DB tier is a strict no-op rather
    than an exception in the hot config path.
    """
    try:
        from django.apps import apps  # noqa: PLC0415

        model = apps.get_model("core", "ConfigSetting")
        return dict(model.objects.overrides_for_scope(""))
    except Exception:  # noqa: BLE001 — fail safe: any read failure => no DB override tier.
        return {}


def _load_overlay_rows(overlay_name: str = "") -> dict[str, Any]:
    """Read the active overlay's ``{key: value}`` rows, alias-tolerant, or ``{}``.

    Matches the row's scope to *overlay_name* canonical-alias-tolerantly (a row
    under either the short alias or the ``t3-``-prefixed entry-point name resolves
    for the active overlay). An exact-name match wins over an alias match. Same
    fail-safe-to-``{}`` posture as :func:`_load_global_rows`.
    """
    if not overlay_name:
        return {}
    try:
        from django.apps import apps  # noqa: PLC0415

        model = apps.get_model("core", "ConfigSetting")
        canonical = OverlayEntry.canonical_overlay_name(overlay_name)
        scope_values: dict[str, dict[str, Any]] = {}
        for scope, key, value in model.objects.exclude(scope="").values_list("scope", "key", "value"):
            if scope == overlay_name or OverlayEntry.canonical_overlay_name(scope) == canonical:
                scope_values.setdefault(scope, {})[key] = value
        return scope_values.get(overlay_name) or next(iter(scope_values.values()), {})
    except Exception:  # noqa: BLE001 — fail safe: any read failure => no DB override tier.
        return {}


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


_AUTONOMY_COLLAPSED_GATE_VALUES: dict[str, Any] = {
    "on_behalf_post_mode": OnBehalfPostMode.IMMEDIATE,
    "require_human_approval_to_merge": False,
    "require_human_approval_to_answer": False,
}


_AUTONOMOUS_TIERS: frozenset[Autonomy] = frozenset({Autonomy.NOTIFY, Autonomy.FULL})


def _toml_home(key: str) -> bool:
    """Whether *key* is a TOML-home ``UserSettings`` field (#1775 partition).

    A DB-home key (or an unknown one) returns ``False`` so the per-overlay TOML
    override layer drops it — its authoritative tier is the ``ConfigSetting``
    store, never the ``[overlays.<name>]`` table.
    """
    return SETTING_HOMES.get(key) is SettingHome.TOML


def _global_pinned_fields(config: TeaTreeConfig) -> set[str]:
    """Names of settings the user explicitly pinned at the GLOBAL scope (#1775).

    A *global* explicit value is a deliberate per-gate opinion for the three
    approval gates and still wins over the autonomy collapse — except for
    ``mode``: a global ``mode`` is a workspace-wide default, not a statement
    about an autonomous overlay, so it must NOT defeat the autonomy ``mode =
    auto`` pin (a common ``mode = "interactive"`` global would otherwise leave a
    ``full``/``notify`` overlay half-autonomous — gates relaxed but the merge
    path still gated on ``mode == AUTO``). ``_apply_autonomy`` only checks
    ``hard_pinned`` for ``mode``, so a global ``mode`` here is harmless. A
    *per-overlay*/env ``mode`` arrives via the override layer (``hard_pinned``)
    and DOES win.

    The three approval gates are now DB-home (#1775), so a *global* pin for them
    is a GLOBAL-scope (``scope=""``) ``ConfigSetting`` row, NOT a ``[teatree]``
    TOML key (a DB-home key left in ``[teatree]`` is ignored on read). A TOML-home
    global key still counts via the ``[teatree]`` table.
    """
    pinned: set[str] = set()
    teatree = config.raw.get("teatree", {})
    if isinstance(teatree, dict):
        pinned |= set(teatree)
    pinned |= set(_load_global_rows())
    return pinned


def _apply_autonomy(settings: UserSettings, *, hard_pinned: set[str], global_pinned: set[str]) -> UserSettings:
    """Collapse the three approval gates for an autonomous tier (``full`` / ``notify``).

    Both autonomous tiers fill only the gates the user left unpinned and pin
    ``mode`` to ``auto`` (the merge-autonomy path is gated on ``mode == AUTO``,
    so a ``full``/``notify`` overlay that forgot ``mode`` would otherwise be a
    silent no-op). The ``notify`` tier additionally derives
    ``notify_on_behalf = True`` so every on-behalf action DMs the user.
    ``babysit`` is a no-op — every gate keeps its resolved value.

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
    per-overlay ``[overlays.<name>]`` override, then the global
    ``[teatree]`` value in ``~/.teatree.toml``, then the ``UserSettings``
    default of 720.

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
