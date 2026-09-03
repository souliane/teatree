"""This box's settings, loops, presets and schedules as one comparable JSON document.

Every key, choice list, default, group and seed field is read from teatree's OWN registries,
so a setting added to the schema shows up here with no edit to this module.

**There is no private mode.** The payload is served over HTTP to another instance, so it
structurally cannot carry a raw secret: a withheld value is replaced by
:func:`~teatree.core.settings_snapshot.serialisation.redaction_stub` — the reason plus a
digest of the real value — which still lets two boxes be compared for DIFFERENCE without
either one carrying the value. A personal backup that does want raw values is a different
job, and ``config_setting export --include-private`` is where it lives.

The withhold is applied by :func:`~teatree.core.settings_snapshot.withholding.redact` at every
DEPTH of a row's value, not to the row key alone: a registry row's own name says nothing about
the credential coordinates its overlay definitions carry.

The format string and version are the ones the offline comparison page already reads, and
every field name matches it, so a snapshot taken from the endpoint stays readable there.
"""

import datetime as dt
import logging
import platform
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError, version
from operator import itemgetter
from typing import Any, Final

from django.db import connections

from teatree.config import (
    ALL_KNOWN_CONFIG_SETTINGS,
    SAFETY_POSTURE_KEYS,
    SETTING_HOMES,
    discover_active_overlay,
    discover_overlays,
    effective_default,
    get_effective_settings,
    is_feature_flag,
    provenance,
)
from teatree.config.schema import setting_meta
from teatree.config.seed_defaults import SEED_ROW_FIELDS, SEED_TABLES, SHIPPED_ONLY_FIELDS, shipped_seed_table
from teatree.config.setting_groups import UNGROUPED_PATH, group_outline
from teatree.config.stored_row_health import stored_row_kind
from teatree.core.config_interchange.migration import export_db_to_toml
from teatree.core.config_interchange.secret_guard import redaction_reason, resolve_export_scan_terms
from teatree.core.config_interchange.seed_tables import live_seed_rows, seed_models
from teatree.core.models import ConfigSetting
from teatree.core.setting_control import TYPE_KINDS, SettingControl, value_kind
from teatree.core.settings_snapshot.fingerprint import Shape, SnapshotError, Warnings, build_fingerprint
from teatree.core.settings_snapshot.serialisation import ROW_KEY_ATTR, Json, field_names, serialise
from teatree.core.settings_snapshot.withholding import redact

logger = logging.getLogger(__name__)

SNAPSHOT_FORMAT: Final = "teatree-settings-snapshot"
FORMAT_VERSION: Final = 1

#: The one ConfigSetting row whose value IS the overlay registry.
OVERLAY_REGISTRY_KEY: Final = "overlays"

_OUTSIDE_INTERCHANGE_NOTE: Final = "outside SEED_ROW_FIELDS — import rejects it"
_SHIPPED_ONLY_NOTE: Final = "shipped-only field — tune it in config/defaults.toml"

_scope_key: Final = itemgetter("scope", "key")
_redaction_key: Final = itemgetter("scope", "key", "path")


def build_snapshot(label: str, note: str = "") -> dict[str, Any]:
    """The whole snapshot for this box, format v1 — a required source raises :class:`SnapshotError`."""
    warn = Warnings()
    controls = {key: SettingControl(key) for key in sorted(ALL_KNOWN_CONFIG_SETTINGS)}
    shapes: dict[str, Shape] = {key: (one.annotation_text, one.raw_choices) for key, one in controls.items()}
    instance = _instance(label, note, warn)
    registry, values, notices = _registry_and_values(controls, instance["overlay"], warn)
    return {
        "format": SNAPSHOT_FORMAT,
        "format_version": FORMAT_VERSION,
        "captured_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "includes_private": False,
        "instance": instance,
        "fingerprint": build_fingerprint(shapes, warn),
        "registry": registry,
        "values": values,
        "redacted": notices["redacted"],
        "omitted": notices["omitted"],
        "export_toml": warn.optional("config_interchange.migration.export_db_to_toml", _export_toml, ""),
        "capture_warnings": sorted(set(warn.messages)),
    }


def _instance(label: str, note: str, warn: Warnings) -> dict[str, Any]:
    facts = {
        "overlays_installed": warn.optional("config.discover_overlays", _installed_overlays, []),
        "overlay_scopes": warn.optional("core.models.ConfigSetting scopes", _overlay_scopes, []),
        "overlay_registry": warn.optional("ConfigSetting['overlays'] registry", _overlay_registry, []),
    }
    return {
        "label": label,
        "note": note,
        "overlay": warn.optional("config.discover_active_overlay", _active_overlay, ""),
        **facts,
        "teatree_version": warn.optional("importlib.metadata.version('teatree')", _teatree_version, "unknown"),
        "python": platform.python_version(),
        "control_db": warn.optional("django control DB path", _control_db, ""),
    }


def _registry_and_values(
    controls: Mapping[str, SettingControl], overlay: str, warn: Warnings
) -> tuple[dict[str, Any], dict[str, Any], dict[str, list[dict[str, str]]]]:
    keys = list(controls)
    terms = warn.optional("secret_guard.resolve_export_scan_terms", lambda: tuple(resolve_export_scan_terms()), ())
    defaults = _default_values(keys, terms, warn)
    settings_registry = _settings_registry(controls, defaults, warn)
    stored, redacted, omitted = _stored_block(terms, warn)
    _mark_redacted_unsyncable(settings_registry, redacted)
    seed_registry, seed_values, seed_shipped = _seed_block(terms, warn)
    # Every scope a row could be planned against: the global one, the active overlay, and any
    # scope that already holds a stored row. `env` outranks all of them, which is the point.
    scopes = sorted({"", overlay, *stored})
    values = {
        "settings": stored,
        "effective": _effective_values(keys, overlay, terms, warn),
        "defaults": defaults,
        "provenance": warn.optional("config.provenance.resolve_settings", lambda: _provenance(keys, scopes), {}),
        "seed": seed_values,
        "seed_shipped": seed_shipped,
    }
    return {"settings": settings_registry, "seed": seed_registry}, values, {"redacted": redacted, "omitted": omitted}


def _settings_registry(
    controls: Mapping[str, SettingControl], defaults: Mapping[str, Any], warn: Warnings
) -> dict[str, dict[str, Any]]:
    keys = list(controls)
    groups = warn.optional("config.setting_groups.group_outline", lambda: _group_labels(keys), {})
    homes = warn.optional("config.SETTING_HOMES", _setting_homes, {})
    flags = warn.optional("config.is_feature_flag", lambda: _flag_keys(keys), frozenset())
    meta = warn.optional("config.schema.setting_meta", lambda: _setting_metas(keys), {})
    secrets = warn.optional("secret_guard.redaction_reason", lambda: _key_secret_reasons(keys), {})
    entries = {}
    for key, control in controls.items():
        category, registry = meta.get(key, ("", ""))
        reason = secrets.get(key, "")
        entries[key] = {
            "kind": control.kind,
            "annotation": control.annotation_text,
            "choices": [serialise(choice) for choice in control.raw_choices] or None,
            "default": defaults.get(key),
            "category": category,
            "registry": registry,
            "help": control.help_text,
            "group": groups.get(key, ""),
            "home": homes.get(key, ""),
            "safety_posture": key in SAFETY_POSTURE_KEYS,
            "feature_flag": key in flags,
            "syncable": not reason,
            "sync_note": _secret_note(reason),
        }
    return entries


def _secret_note(reason: str) -> str:
    return f"secret ({reason}) — the import rejects it" if reason else ""


def _withheld_note(entry: Mapping[str, str]) -> str:
    """A whole-row withhold and a withheld LEAF fail an import differently — say which."""
    if not entry["path"]:
        return _secret_note(entry["reason"])
    return f"a withheld value sits at {entry['path']} ({entry['reason']}) — an import would write the stub"


def _mark_redacted_unsyncable(registry: dict[str, dict[str, Any]], redacted: Sequence[Mapping[str, str]]) -> None:
    """A row carrying a withheld value can never ride an import, whatever its declaration says."""
    for entry in redacted:
        setting = registry.get(entry["key"])
        if setting is None:
            continue
        setting["syncable"] = False
        setting["sync_note"] = setting["sync_note"] or _withheld_note(entry)


def _guarded(key: str, value: object, terms: tuple[str, ...]) -> Json:
    """*value* serialised, with anything withheld stubbed — the SAME rule the stored rows take.

    The resolved and default tiers are guarded exactly like the stored ones. They are not
    stored rows, but they are the same values: a secret resolved from env is still a secret,
    and this payload is served to another instance.
    """
    return redact(key, value, terms)[0]


def _default_values(keys: Sequence[str], terms: tuple[str, ...], warn: Warnings) -> dict[str, Any]:
    values, failed = {}, []
    for key in keys:
        try:
            values[key] = _guarded(key, effective_default(key), terms)
        except Exception:
            logger.exception("effective_default(%r) failed", key)
            failed.append(key)
    if failed:
        warn.add(f"config.effective_default failed for {len(failed)} key(s): {', '.join(sorted(failed)[:5])}")
    return values


def _effective_values(keys: Sequence[str], overlay: str, terms: tuple[str, ...], warn: Warnings) -> dict[str, Any]:
    def resolve() -> dict[str, Any]:
        settings = get_effective_settings(overlay or None)
        return {key: _guarded(key, getattr(settings, key), terms) for key in keys if hasattr(settings, key)}

    return warn.optional("config.get_effective_settings", resolve, {})


def _provenance(keys: Sequence[str], scopes: Sequence[str]) -> dict[str, dict[str, str]]:
    """Which tier each key actually resolves from, per scope.

    teatree resolves env -> DB(overlay) -> DB(global) -> overlay code default -> shipped file
    -> code default. So a key the target box reads from a ``T3_*`` variable does not change
    when a DB row is imported for it: the import succeeds and nothing observable happens.
    Recording the tier is what lets the plan say so instead of emitting an inert write.
    """
    resolved: dict[str, dict[str, str]] = {}
    for scope in scopes:
        rows = provenance.resolve_settings(keys, scope=scope)
        resolved[scope] = {key: str(getattr(row.source, "value", row.source)) for key, row in sorted(rows.items())}
    return resolved


def _stored_block(
    terms: tuple[str, ...], warn: Warnings
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    return warn.optional("core.models.ConfigSetting store", lambda: _stored_settings(terms), ({}, [], []))


def _stored_settings(
    terms: tuple[str, ...],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    values: dict[str, dict[str, Any]] = {}
    redacted: list[dict[str, str]] = []
    omitted: list[dict[str, str]] = []
    for row in ConfigSetting.objects.all():
        scope, key = str(row.scope), str(row.key)
        if note := stored_row_kind(key):
            omitted.append({"scope": scope, "key": key, "reason": note})
            continue
        body, withheld = redact(key, row.value, terms)
        redacted.extend({"scope": scope, "key": key, "path": one.path, "reason": one.reason} for one in withheld)
        values.setdefault(scope, {})[key] = body
    return values, sorted(redacted, key=_redaction_key), sorted(omitted, key=_scope_key)


def _group_labels(keys: Sequence[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    path: list[str] = []
    for section in group_outline(sorted(keys), key_of=lambda key: key):
        for heading in section.headings:
            del path[heading.depth - 1 :]
            path.append(str(heading.label))
        current = tuple(path[: section.depth])
        labels.update(dict.fromkeys(section.rows, "" if current == tuple(UNGROUPED_PATH) else " / ".join(current)))
    return labels


def _setting_homes() -> dict[str, str]:
    return {key: str(getattr(home, "value", home)) for key, home in SETTING_HOMES.items()}


def _setting_metas(keys: Sequence[str]) -> dict[str, tuple[str, str]]:
    metas = {}
    for key in keys:
        try:
            meta = setting_meta(key)
        except (KeyError, StopIteration):
            continue
        category, registry = meta.category, meta.registry
        metas[key] = (str(getattr(category, "value", category)), str(getattr(registry, "value", registry)))
    return metas


def _flag_keys(keys: Sequence[str]) -> frozenset[str]:
    return frozenset(key for key in keys if is_feature_flag(key))


def _key_secret_reasons(keys: Sequence[str]) -> dict[str, str]:
    # terms=() so only the value-independent key classes match — the import's own reject rule
    return {key: reason for key in keys if (reason := redaction_reason(key, None, ()))}


def _seed_block(terms: tuple[str, ...], warn: Warnings) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    models = warn.optional("config_interchange.seed_tables.seed_models", seed_models, {})
    live = _seed_values(models, terms, warn)
    shipped = warn.optional("config.seed_defaults.shipped_seed_table", lambda: _shipped_seed_values(terms), {})
    registry = {}
    for table in SEED_TABLES:
        fields = warn.optional(
            f"seed registry for {table!r}",
            lambda name=table: _seed_table_fields(name, models.get(name), live.get(name, {}), shipped.get(name, {})),
            {},
        )
        registry[table] = {"fields": fields}
    return registry, live, shipped


def _seed_values(models: Mapping[str, Any], terms: tuple[str, ...], warn: Warnings) -> dict[str, dict[str, dict]]:
    """Every seed row's fields, guarded exactly like a setting row's.

    A preset's ``entries`` table maps setting keys to values, so it can hold a credential
    coordinate under a field name (``entries``) that says nothing about it — the same shape as
    the ``overlays`` registry, on the surface next door.
    """
    live: dict[str, dict[str, dict[str, Any]]] = {}
    for table in SEED_TABLES:
        rows = warn.optional(f"live_seed_rows({table!r})", lambda name=table: live_seed_rows(name), {})
        carried = {
            name: {key: _guarded(key, value, terms) for key, value in body.items()} for name, body in rows.items()
        }
        extras = warn.optional(
            f"seed extras for {table!r}", lambda name=table: _seed_extras(name, models.get(name), terms), {}
        )
        live[table] = {name: {**carried.get(name, {}), **extras.get(name, {})} for name in sorted({*carried, *extras})}
    return live


def _seed_extras(table: str, model: object, terms: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """The seed rows' fields the interchange cannot carry — captured read-only, so they can diff."""
    manager = getattr(model, "objects", None)
    if manager is None:
        return {}
    names = _extra_field_names(model, SEED_ROW_FIELDS.get(table, {}))
    names += [name for name in SHIPPED_ONLY_FIELDS.get(table, ()) if hasattr(model, name)]
    return {
        str(getattr(row, ROW_KEY_ATTR, "")): {name: _guarded(name, getattr(row, name, None), terms) for name in names}
        for row in manager.all()
    }


def _shipped_seed_values(terms: tuple[str, ...]) -> dict[str, dict[str, dict[str, Any]]]:
    # Guarded like the live tier so the two compare like for like: a stub on one side and the
    # raw value on the other would read as a difference the boxes do not actually have.
    return {
        table: {
            name: {key: _guarded(key, value, terms) for key, value in body.items()}
            for name, body in shipped_seed_table(table).items()
        }
        for table in SEED_TABLES
    }


def _extra_field_names(model: object, carried: Mapping[str, tuple[str, type]]) -> list[str]:
    already = {attr for attr, _python_type in carried.values()} | {ROW_KEY_ATTR}
    return [name for name in field_names(model, relations=True) if name not in already]


def _seed_table_fields(
    table: str, model: object, live: Mapping[str, Any], shipped: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    carried = SEED_ROW_FIELDS.get(table, {})
    fields: dict[str, dict[str, Any]] = {
        name: {"kind": TYPE_KINDS.get(python_type, "unknown"), "attr": attr, "syncable": True, "sync_note": ""}
        for name, (attr, python_type) in carried.items()
    }
    for name in SHIPPED_ONLY_FIELDS.get(table, ()):
        fields[name] = {
            "kind": _observed_kind(name, live, shipped),
            "attr": name if hasattr(model, name) else "",
            "syncable": False,
            "sync_note": _SHIPPED_ONLY_NOTE,
        }
    extras = {*_extra_field_names(model, carried), *(name for body in live.values() for name in body)} - set(fields)
    for name in sorted(extras):
        fields[name] = {
            "kind": _observed_kind(name, live, shipped),
            "attr": name,
            "syncable": False,
            "sync_note": _OUTSIDE_INTERCHANGE_NOTE,
        }
    return fields


def _observed_kind(field_name: str, *sources: Mapping[str, Mapping[str, Any]]) -> str:
    for source in sources:
        for body in source.values():
            if (value := body.get(field_name)) is not None:
                return value_kind(value)
    return "unknown"


def _installed_overlays() -> list[dict[str, str]]:
    rows = [{"name": str(entry.name), "path": str(getattr(entry, "project_path", ""))} for entry in discover_overlays()]
    return sorted(rows, key=itemgetter("name"))


def _overlay_scopes() -> list[str]:
    # "" is a scope, not a gap: it is where a global override lives, so it stays in the list
    return sorted({str(row.scope) for row in ConfigSetting.objects.all()})


def _overlay_registry() -> list[str]:
    names: set[str] = set()
    for row in ConfigSetting.objects.all():
        if str(row.key) == OVERLAY_REGISTRY_KEY and isinstance(row.value, Mapping):
            names.update(str(name) for name in row.value)
    return sorted(names)


def _active_overlay() -> str:
    entry = discover_active_overlay()
    return "" if entry is None else str(entry.name)


def _teatree_version() -> str:
    try:
        return version("teatree")
    except PackageNotFoundError:
        return "unknown"


def _control_db() -> str:
    return str(connections["default"].settings_dict.get("NAME", ""))


def _export_toml() -> str:
    return str(export_db_to_toml(include_private=False).toml)


__all__ = ["FORMAT_VERSION", "OVERLAY_REGISTRY_KEY", "SNAPSHOT_FORMAT", "SnapshotError", "build_snapshot"]
