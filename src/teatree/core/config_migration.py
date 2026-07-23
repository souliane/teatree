"""``ConfigSetting`` store -> TOML export — the personal-backup serialiser.

``config_setting export`` dumps the DB config store to TOML text (stdout or a
file) so an operator has a human-readable, re-importable backup of their private
config. It is NOT the config home — teatree reads config only from the DB — it is
a one-way dump for backup/inspection. The secret guard withholds customer/brand
rows from a SHARED export by default (``SECRET_SETTINGS`` + a live banned-term
content scan); ``--include-private`` exports everything for a personal backup.
"""

import json
from dataclasses import dataclass
from typing import Any

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config.registries import REGISTRY_KEYS
from teatree.config.secret_settings import PERSONAL_IDENTIFIERS, SECRET_SETTINGS, is_credential_reference
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import ConfigValue
from teatree.hooks.term_match import matched_term

GLOBAL_SCOPE = ""


def _scope_label(scope: str) -> str:
    """Human label for a scope: ``global`` for the empty scope else ``overlay '<name>'``."""
    return "global" if not scope else f"overlay {scope!r}"


@dataclass(frozen=True)
class RedactedRow:
    """One export row withheld by the secret guard, with the reason it was dropped."""

    scope: str
    key: str
    reason: str  # "private-key" / "credential-coordinate" / "personal-identifier" / "banned-term:<term>"


@dataclass(frozen=True)
class ConfigExport:
    """A config-store export: the TOML text plus the rows the secret guard withheld."""

    toml: str
    redacted: tuple[RedactedRow, ...]


@dataclass
class _ExportGuard:
    """The secret-guard context threaded through every export emitter.

    ``include_private`` exports everything (a personal backup); otherwise each row is
    scanned against ``terms`` + ``SECRET_SETTINGS`` and a withheld one is appended to the
    shared ``redacted`` accumulator. Bundled so the emitters stay within the arg-count cap.
    """

    include_private: bool
    terms: tuple[str, ...]
    redacted: list[RedactedRow]


def _resolve_export_scan_terms() -> tuple[str, ...]:
    """Banned terms + brands for the export content scan; fails safe to empty.

    Read straight from the DB store (the codename lists' home) via the Django-free
    ``cold_reader``, so a shared export scans the operator's configured customer/
    brand terms without any file. An unconfigured store yields no terms (empty).
    """
    # Deferred (PLC0415): importing `teatree.config` at module scope eagerly
    # loads its heavy package __init__; keep this module's import light.
    from teatree.config import cold_reader  # noqa: PLC0415 — deferred: call-time import, kept lazy

    terms: list[str] = []
    for key in ("banned_terms", "banned_brands"):
        value = cold_reader.read_setting(key)
        if isinstance(value, list):
            terms.extend(str(t) for t in value if str(t).strip())
    return tuple(terms)


def _redaction_reason(key: str, value: ConfigValue, terms: tuple[str, ...]) -> str | None:
    """Why this row must not be shared, else None.

    Four withhold classes, first match wins: an explicit private key
    (``SECRET_SETTINGS``); a credential coordinate (the SAME suffix rule the dashboard
    credential band uses — ``anthropic_oauth_pass_paths`` / ``*_credential_entry`` /
    ``*_token_ref`` etc.); a personal identifier (``slack_user_id`` /
    ``slack_user_channel`` / ``availability_schedule``); or a value carrying a banned
    customer/brand term. The credential + personal classes close the F2 leak where
    pass-store coordinates and personal handles shipped by default on export.
    """
    if key in SECRET_SETTINGS:
        return "private-key"
    if is_credential_reference(key):
        return "credential-coordinate"
    if key in PERSONAL_IDENTIFIERS:
        return "personal-identifier"
    hit = matched_term(f"{key} {json.dumps(value, default=str)}", terms)
    return f"banned-term:{hit}" if hit else None


def _exportable_rows(rows: dict[str, ConfigValue], scope: str, *, guard: _ExportGuard) -> dict[str, ConfigValue]:
    """Drop secret/tainted rows (recording each in ``guard.redacted``) unless include_private."""
    if guard.include_private:
        return rows
    kept: dict[str, ConfigValue] = {}
    for key, value in rows.items():
        reason = _redaction_reason(key, value, guard.terms)
        if reason is None:
            kept[key] = value
        else:
            guard.redacted.append(RedactedRow(scope, key, reason))
    return kept


def export_db_to_toml(
    overlay: str | None = None,
    *,
    include_private: bool = False,
    scan_terms: tuple[str, ...] | None = None,
) -> ConfigExport:
    """Serialise the ``ConfigSetting`` store to TOML — a personal, re-importable backup.

    Global-scope settings render under ``[teatree]``; each overlay renders under
    ``[overlays.<name>]`` (its registry DEFINITIONS merged with its per-overlay SETTING
    scope rows); the ``e2e_repos`` registry renders as ``[e2e_repos.<name>]`` tables. The
    two registry keys are NEVER dumped under ``[teatree]`` (they are not ``UserSettings``
    fields). With *overlay* the dump is scoped to that one overlay's ``[overlays.<name>]``
    table; omitted, it dumps the global scope plus every overlay scope plus the e2e-repos
    registry.

    By DEFAULT the secret guard withholds any row that is a known-private key
    (``SECRET_SETTINGS``) OR whose key/value contains a banned customer/brand term
    (``scan_terms``, resolved from the live config when not supplied) — so a SHARED
    export cannot leak customer data even though the private DB store keeps it.
    ``include_private`` exports everything for a personal, never-shared backup. The
    withheld rows ride back on the result so the caller can warn what it dropped.
    """
    terms = scan_terms if scan_terms is not None else _resolve_export_scan_terms()
    guard = _ExportGuard(include_private=include_private, terms=terms, redacted=[])
    document = tomlkit.document()
    all_global = ConfigSetting.objects.overrides_for_scope(GLOBAL_SCOPE)
    overlays_registry = _registry_value(all_global, "overlays")
    e2e_repos_registry = _registry_value(all_global, "e2e_repos")

    if overlay is not None:
        scoped_registry = {overlay: overlays_registry[overlay]} if overlay in overlays_registry else {}
        _emit_overlay_tables(document, [overlay], scoped_registry, guard=guard)
        return ConfigExport(tomlkit.dumps(document), tuple(guard.redacted))

    # The registry keys are rendered as their own top-level tables below, never under
    # ``[teatree]`` (they are NOT ``UserSettings`` fields) — exclude them from the
    # global settings table so the dump re-imports cleanly.
    settings_global = {key: value for key, value in all_global.items() if key not in REGISTRY_KEYS}
    global_rows = _exportable_rows(settings_global, GLOBAL_SCOPE, guard=guard)
    if global_rows:
        document["teatree"] = _toml_table(global_rows)
    scopes = list(
        ConfigSetting.objects.exclude(scope=GLOBAL_SCOPE).order_by("scope").values_list("scope", flat=True).distinct()
    )
    _emit_overlay_tables(document, scopes, overlays_registry, guard=guard)
    _emit_e2e_repos_tables(document, e2e_repos_registry, guard=guard)
    return ConfigExport(tomlkit.dumps(document), tuple(guard.redacted))


def _registry_value(global_rows: dict[str, ConfigValue], key: str) -> dict[str, Any]:
    """The stored registry dict for *key* in the global rows, or ``{}`` when absent/malformed."""
    value = global_rows.get(key)
    return value if isinstance(value, dict) else {}


def _toml_table(rows: dict[str, ConfigValue]) -> tomlkit_items.Table:
    """A ``[table]`` of *rows*, each native value rendered as its TOML scalar."""
    table = tomlkit.table()
    for key, value in rows.items():
        table[key] = value
    return table


def _emit_overlay_tables(
    document: tomlkit.TOMLDocument,
    scopes: list[str],
    overlays_registry: dict[str, Any],
    *,
    guard: _ExportGuard,
) -> None:
    """Attach an ``[overlays.<name>]`` sub-table per overlay, merging definitions + settings.

    Each table is the union of the overlay's DEFINITION keys (from the ``overlays``
    registry row — ``path`` / ``class`` / ...) and its per-overlay SETTING overrides
    (its scope rows). The names are the registry overlays UNION the setting scopes,
    deduped order-stable. The ``overlays`` super-table is added only when at least one
    overlay has rows that survive the secret guard, so an empty store stays an empty
    document rather than a bare ``[overlays]`` header.
    """
    overlays = tomlkit.table(is_super_table=True)
    emitted = False
    for name in dict.fromkeys([*overlays_registry, *scopes]):
        merged = {**overlays_registry.get(name, {}), **ConfigSetting.objects.overrides_for_scope(name)}
        rows = _exportable_rows(merged, name, guard=guard)
        if rows:
            overlays[name] = _toml_table(rows)
            emitted = True
    if emitted:
        document["overlays"] = overlays


def _emit_e2e_repos_tables(
    document: tomlkit.TOMLDocument,
    e2e_repos_registry: dict[str, Any],
    *,
    guard: _ExportGuard,
) -> None:
    """Attach an ``[e2e_repos.<name>]`` sub-table per registered E2E repo.

    The inverse of ``load_e2e_repos`` reading ``raw["e2e_repos"]`` — each entry's
    ``url`` / ``branch`` / ``e2e_dir`` rendered as its own table. The super-table is
    added only when a repo has rows surviving the secret guard.
    """
    repos = tomlkit.table(is_super_table=True)
    emitted = False
    for name, entry in e2e_repos_registry.items():
        if not isinstance(entry, dict):
            continue
        rows = _exportable_rows(entry, f"e2e_repos.{name}", guard=guard)
        if rows:
            repos[name] = _toml_table(rows)
            emitted = True
    if emitted:
        document["e2e_repos"] = repos
