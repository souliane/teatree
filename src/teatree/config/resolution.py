"""Effective-settings resolution — env + DB + per-overlay overrides + the autonomy collapse.

``get_effective_settings`` (the single resolver both the active-overlay and
named-overlay paths share), ``cadence_seconds``, the autonomy-collapse
(``_apply_autonomy``), and the per-setting toml resolvers ``load_config`` uses.
Split out of the package module for the module-health LOC cap; re-exported from
``teatree.config``.

The override tiers it layers on the file-tier ``UserSettings`` (later wins):
per-overlay ``[overlays.<name>]`` TOML, then the #1775 DB tier
(``_db_setting_overrides`` reading ``ConfigSetting`` rows), then ``T3_*`` env.
The DB read is fail-safe (an absent/empty table or unconfigured Django yields no
overrides) so an empty table is a provable no-op.

``_resolve_autonomy`` / ``_resolve_speed`` are collapsed into the generic
``_resolve_enum_setting`` registry — both are plain enum-or-default reads.
``_resolve_on_behalf_post_mode`` (returns a tuple) and
``_resolve_slack_voice_classifier_mode`` (reads a nested table) stay bespoke.
"""

import os
from dataclasses import replace
from typing import Any, Protocol

import teatree.config as _facade
from teatree.config.discovery import _active_overlay_entry
from teatree.config.enums import Autonomy, Mode, OnBehalfPostMode
from teatree.config.settings import (
    ENV_SETTING_OVERRIDES,
    OVERLAY_OVERRIDABLE_SETTINGS,
    OverlayEntry,
    TeaTreeConfig,
    UserSettings,
)
from teatree.config_speak import speak_from_subtable
from teatree.types import SlackVoiceClassifierMode, SpeakConfig


class _ParsableEnum(Protocol):
    """A config enum that validates an explicit value through a ``parse`` classmethod."""

    @classmethod
    def parse(cls, value: str) -> "_ParsableEnum": ...


def _resolve_enum_setting[E: _ParsableEnum](teatree: dict[str, Any], key: str, enum: type[E], default: E) -> E:
    """Resolve a plain enum-or-default ``[teatree]`` setting.

    Absent → the conservative *default*; a typo raises via ``enum.parse`` (never
    a silent downgrade). The per-overlay override and any env var are applied
    later in :func:`get_effective_settings`. Collapses the former
    ``_resolve_autonomy`` / ``_resolve_speed`` — both are plain enum-or-default
    reads — into one generic resolver.
    """
    raw = teatree.get(key)
    return enum.parse(raw) if raw is not None else default


def _resolve_slack_voice_classifier_mode(teatree: dict[str, Any]) -> SlackVoiceClassifierMode:
    """Resolve ``slack_voice_classifier_mode`` from ``[teatree]`` (#1395).

    Accepts either a flat key ``[teatree] slack_voice_classifier_mode``
    or a nested ``[teatree.publish_gates] slack_voice_classifier_mode``
    (the table the issue brief sketches for grouping future
    pre-publish gates). The flat key wins when both are present;
    falling back through the nested table then to the conservative
    default keeps the backward-compat upgrade path clean — existing
    configs that don't know about the gate inherit ``WARN`` (log the
    mismatch, allow the post) rather than ``STRICT`` (refuse).
    """
    flat = teatree.get("slack_voice_classifier_mode")
    if flat is not None:
        return SlackVoiceClassifierMode.parse(flat)
    nested = teatree.get("publish_gates")
    if isinstance(nested, dict):
        scoped = nested.get("slack_voice_classifier_mode")
        if scoped is not None:
            return SlackVoiceClassifierMode.parse(scoped)
    return SlackVoiceClassifierMode.WARN


def _resolve_on_behalf_post_mode(teatree: dict[str, Any]) -> tuple[OnBehalfPostMode, bool]:
    """Resolve ``on_behalf_post_mode`` from a ``[teatree]`` toml table.

    Precedence:

    1.  Explicit ``on_behalf_post_mode = "..."`` always wins.
    2.  Legacy ``ask_before_post_on_behalf = true/false`` maps to
        :attr:`OnBehalfPostMode.ASK` / :attr:`OnBehalfPostMode.IMMEDIATE`.
    3.  Neither set → :attr:`OnBehalfPostMode.DRAFT_OR_ASK` (new default).

    Returns ``(mode, derived_ask_bool)`` so the legacy boolean field on
    ``UserSettings`` stays consistent with the resolved mode for the one
    deprecation release we keep it around.
    """
    raw_mode = teatree.get("on_behalf_post_mode")
    if raw_mode is not None:
        mode = OnBehalfPostMode.parse(raw_mode)
    elif "ask_before_post_on_behalf" in teatree:
        # Backward-compat alias: explicit legacy boolean → matching mode.
        legacy = bool(teatree["ask_before_post_on_behalf"])
        mode = OnBehalfPostMode.ASK if legacy else OnBehalfPostMode.IMMEDIATE
    else:
        mode = OnBehalfPostMode.DRAFT_OR_ASK
    # Derived legacy boolean: ASK/DRAFT_OR_ASK both block colleague-visible
    # publishing (only the draft-form variant publishes autonomously under
    # DRAFT_OR_ASK), so they map to "ask before post" = True.
    derived_ask = mode is not OnBehalfPostMode.IMMEDIATE
    return mode, derived_ask


def get_effective_settings(overlay_name: str | None = None) -> UserSettings:
    """Return the user settings with env, DB, and per-overlay overrides applied.

    Resolution per field (first match wins): ``T3_*`` env var (see
    ``ENV_SETTING_OVERRIDES``), the DB override tier (``ConfigSetting`` rows,
    #1775), the active overlay's override from ``[overlays.<name>]``, the global
    ``[teatree]`` value, the ``UserSettings`` dataclass default — i.e.

        env -> DB -> per-overlay TOML -> global [teatree] -> dataclass default.

    The DB tier is the first slice of "move config to the database" (#1775): a
    ``ConfigSetting`` row for a key in ``OVERLAY_OVERRIDABLE_SETTINGS`` overrides
    the file value but is still beaten by an explicit env var. An EMPTY table is a
    provable no-op — :func:`_db_setting_overrides` returns ``{}`` — so nothing
    regresses during the migration window. The read fails safe to ``{}`` whenever
    Django is not configured or the table does not exist yet.

    The active overlay is resolved via ``T3_OVERLAY_NAME`` first (matches
    ``get_overlay()``), then cwd-based discovery, then the single
    installed overlay.

    ``overlay_name`` resolves a SPECIFIC named overlay instead of the active
    one — the loop's scanner-builders fan out over every registered overlay,
    not just the session's. In that mode the env layer is NOT applied; the DB
    tier, the per-overlay ``[overlays.<name>]`` overrides, and the autonomy
    collapse run identically. This is the single resolver both paths share.

    To make an additional setting overridable, add it to
    ``OVERLAY_OVERRIDABLE_SETTINGS`` (per-overlay + DB) or ``ENV_SETTING_OVERRIDES``
    (env); the resolver picks it up generically via ``dataclasses.replace``.
    The one non-generic override is ``speak``: its ``[overlays.<name>.speak]``
    sub-table MERGES onto the base (see :func:`_overlay_speak_override`) rather
    than flat-replacing, so a partial table overrides only the keys it sets.

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
    # Precedence (later wins): per-overlay TOML -> DB tier -> env.
    overrides.update(_db_setting_overrides())
    if overlay_name is None:
        overrides.update(_env_setting_overrides())
    settings = base if not overrides else replace(base, **overrides)
    speak_override = _overlay_speak_override(config, overlay_name, base.speak)
    if speak_override is not None:
        settings = replace(settings, speak=speak_override)
        overrides = {**overrides, "speak": speak_override}
    return _apply_autonomy(settings, hard_pinned=set(overrides), global_pinned=_global_pinned_fields(config))


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
    overrides.update(_db_setting_overrides())
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


def _db_setting_overrides() -> dict[str, Any]:
    """The ``ConfigSetting`` DB override tier (#1775) — scoped to overridable keys.

    Returns ``{field_name: coerced_value}`` for every ``ConfigSetting`` row whose
    ``key`` is a registered ``OVERLAY_OVERRIDABLE_SETTINGS`` field, coercing the
    stored JSON value with that registry's parser so the resolved field keeps its
    declared type. Rows for unknown / non-overridable keys are ignored, so a
    stray row can never silently mutate the resolved settings.

    Fails safe to ``{}`` for INFRASTRUCTURE failures — an empty/absent table, an
    unconfigured Django, or a pre-migration database (the table does not exist
    yet) all yield no overrides, so the file/env source is unchanged. This is the
    #1775 no-regression-during-migration invariant: the table read is on every
    config resolution and must never raise into it (handled in
    :func:`_load_config_setting_rows`).

    A per-row parser failure is a DIFFERENT class: it means a stored value is
    invalid for its setting's type (an out-of-enum ``mode``, a quoted ``"false"``
    for a bool-typed setting). With write-time validation in place
    (``config_setting set`` runs the same registry parser, #258) such a row can
    only exist if the DB was corrupted out of band — so it is raised LOUD with
    the offending key named, not swallowed: a silently-dropped invalid override
    would resolve back to the file/env value with no signal, exactly the kind of
    quiet wrong answer the strict parsing change is meant to foreclose.
    """
    rows = _load_config_setting_rows()
    if not rows:
        return {}
    overrides: dict[str, Any] = {}
    for key, value in rows:
        parser = OVERLAY_OVERRIDABLE_SETTINGS.get(key)
        if parser is None:
            continue
        try:
            overrides[key] = parser(value)
        except (ValueError, TypeError, AttributeError) as exc:
            msg = f"Invalid stored ConfigSetting value for {key!r}: {exc}"
            raise ValueError(msg) from exc
    return overrides


def _load_config_setting_rows() -> list[tuple[str, Any]]:
    """Read every ``ConfigSetting`` ``(key, value)`` pair, or ``[]`` on any failure.

    Reaches the model via Django's app registry (no static ``teatree.core``
    import — that would be a backwards ``platform -> domain`` tach edge). Every
    failure mode of an early/unconfigured read — Django apps not ready, no
    settings, the table missing pre-migration, the DB unreachable — degrades to
    an empty list so the DB tier is a strict no-op rather than an exception in the
    hot config path.
    """
    try:
        from django.apps import apps  # noqa: PLC0415

        model = apps.get_model("core", "ConfigSetting")
        return list(model.objects.values_list("key", "value"))
    except Exception:  # noqa: BLE001 — fail safe: any read failure => no DB override tier.
        return []


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


def _global_pinned_fields(config: TeaTreeConfig) -> set[str]:
    """Names of settings explicitly set in the global ``[teatree]`` toml table.

    A *global* explicit value is a deliberate per-gate opinion for the three
    approval gates and still wins over the autonomy collapse — except for
    ``mode``: a global ``[teatree] mode`` is a workspace-wide default, not a
    statement about an autonomous overlay, so it must NOT defeat the autonomy
    ``mode = auto`` pin (a common ``mode = "interactive"`` global would
    otherwise leave a ``full``/``notify`` overlay half-autonomous — gates
    relaxed but the merge path still gated on ``mode == AUTO``). A *per-overlay*
    ``[overlays.<name>].mode`` arrives via the override layer (``hard_pinned``)
    and DOES win — see :func:`_apply_autonomy`.
    """
    teatree = config.raw.get("teatree", {})
    return set(teatree) if isinstance(teatree, dict) else set()


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
