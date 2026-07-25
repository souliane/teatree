"""``ConfigSetting`` store -> TOML export — the personal-backup serialiser.

``config_setting export`` dumps the DB config store to TOML text (stdout or a
file) so an operator has a human-readable, re-importable backup of their private
config. It is NOT the config home — teatree reads config only from the DB — it is
a one-way dump for backup/inspection. The secret guard withholds customer/brand
rows from a SHARED export by default (``SECRET_SETTINGS`` + a live banned-term
content scan); ``--include-private`` exports everything for a personal backup.
"""

import json
import tomllib
from dataclasses import dataclass
from typing import Any

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config import effective_default
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.registries import REGISTRY_KEYS
from teatree.config.retired_settings import REMOVED_SETTING_KEYS, RENAMED_SETTING_KEYS, removed_setting
from teatree.config.secret_settings import PERSONAL_IDENTIFIERS, SECRET_SETTINGS, is_credential_reference
from teatree.config.setting_registries import OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import ConfigValue
from teatree.hooks.term_match import matched_term

GLOBAL_SCOPE = ""
_TEATREE_TABLE = "teatree"
_OVERLAYS_TABLE = "overlays"
_E2E_REPOS_TABLE = "e2e_repos"


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
    """Every ban-class term for the export content scan; fails safe to empty when unset.

    Delegates to :func:`banned_term_registry.export_scan_terms` — the single home that
    resolves the ban classes registry-first (``leak`` + ``prose_collider`` + ``tone`` +
    ``overlay``; the ``allow`` carve-out is excluded) and falls back to the legacy
    ``banned_terms`` + ``banned_brands`` rows when the registry is unset. Keeping the
    resolution there (rather than reading the legacy rows here) leaves the registry the
    single term-source: a shared export scans the operator's configured customer/brand
    terms without any file, an unconfigured store yields no terms, and a malformed
    registry fails loud exactly like the gates.
    """
    # Deferred (PLC0415): importing `teatree.hooks` at module scope eagerly loads its
    # heavy package __init__; keep this module's import light.
    from teatree.hooks.banned_term_registry import export_scan_terms  # noqa: PLC0415 — deferred: kept lazy

    return export_scan_terms()


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
    """A ``[table]`` of *rows* (key-sorted), each native value rendered as its TOML scalar.

    Sorted so the dump is a deterministic function of the store's CONTENT, not the DB
    insertion order — the property ``export -> import -> export`` byte-stability rests on.
    """
    table = tomlkit.table()
    for key in sorted(rows):
        table[key] = rows[key]
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
    for name in sorted(dict.fromkeys([*overlays_registry, *scopes])):
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
    for name in sorted(e2e_repos_registry):
        entry = e2e_repos_registry[name]
        if not isinstance(entry, dict):
            continue
        rows = _exportable_rows(entry, f"e2e_repos.{name}", guard=guard)
        if rows:
            repos[name] = _toml_table(rows)
            emitted = True
    if emitted:
        document["e2e_repos"] = repos


# ---- import (the inverse of export) -----------------------------------------------------


@dataclass(frozen=True)
class RejectedRow:
    """One import row the validator refused, with the reason it was not stored."""

    scope: str
    key: str
    reason: str  # "unknown key" / "secret (<class>)" / "removed (<why>)" / "invalid: <msg>"


@dataclass(frozen=True)
class ImportedRow:
    """One import row that was (or, under ``dry_run``, would be) written to the store."""

    scope: str
    key: str
    value: ConfigValue


@dataclass(frozen=True)
class ConfigImport:
    """The outcome of an ``import_toml_to_db`` run — all four dispositions, plus the mode.

    ``rejected`` non-empty means the import was REFUSED wholesale: nothing was written,
    even the clean rows, so a partial store can never result from one bad key.
    """

    written: tuple[ImportedRow, ...]
    skipped_default: tuple[ImportedRow, ...]
    folded: tuple[tuple[str, str], ...]  # (retired alias, canonical replacement)
    rejected: tuple[RejectedRow, ...]
    dry_run: bool


def _import_candidates(doc: dict[str, Any]) -> list[tuple[str, str, ConfigValue]]:
    """Flatten a parsed export document into ``(scope, key, value)`` candidate rows.

    Reverses the export layout: the ``[teatree]`` table -> global settings; each
    ``[overlays.<name>]`` table splits into per-overlay SETTING rows (keys in
    ``OVERLAY_OVERRIDABLE_SETTINGS``) and overlay-DEFINITION keys (``path`` / ``class`` /
    …, folded back into the ``overlays`` registry row); each ``[e2e_repos.<name>]`` table
    rebuilds the ``e2e_repos`` registry row.
    """
    candidates: list[tuple[str, str, ConfigValue]] = []
    for key, value in doc.get(_TEATREE_TABLE, {}).items():
        candidates.append((GLOBAL_SCOPE, key, value))
    overlays_registry: dict[str, Any] = {}
    for name, table in doc.get(_OVERLAYS_TABLE, {}).items():
        if not isinstance(table, dict):
            continue
        for key, value in table.items():
            if key in OVERLAY_OVERRIDABLE_SETTINGS:
                candidates.append((name, key, value))
            else:
                overlays_registry.setdefault(name, {})[key] = value
    if overlays_registry:
        candidates.append((GLOBAL_SCOPE, _OVERLAYS_TABLE, overlays_registry))
    e2e_registry: dict[str, Any] = {n: dict(t) for n, t in doc.get(_E2E_REPOS_TABLE, {}).items() if isinstance(t, dict)}
    if e2e_registry:
        candidates.append((GLOBAL_SCOPE, _E2E_REPOS_TABLE, e2e_registry))
    return candidates


def _classify_import_row(key: str, value: ConfigValue, terms: tuple[str, ...]) -> tuple[str, ConfigValue]:
    """Decide one row's disposition: ``("reject", reason)`` / ``("skip"|"write", canonical)``.

    Reject a removed key (loud, no home), an unknown key, and a secret/personal-identifier
    row (reusing the export withhold rule so a shared TOML never smuggles customer data back
    in). Otherwise coerce through the shared write-path validator; a value equal to the key's
    EFFECTIVE default (:func:`~teatree.config.effective_default` — the resolver's own default,
    the same authority the seed-skip consults) is redundant (``skip``), leaving
    ``restore = delete row`` intact. An adopted-live ``defaults.toml`` value that diverges
    from the code default is NOT redundant, so it is written rather than skipped-then-silently
    resolving back to the code default (P1-A).
    """
    if key in REMOVED_SETTING_KEYS:
        entry = removed_setting(key)
        return ("reject", f"removed ({entry.reason if entry is not None else 'the setting was removed'})")
    if key not in ALL_KNOWN_CONFIG_SETTINGS:
        return ("reject", "unknown key")
    if (secret := _redaction_reason(key, value, terms)) is not None:
        return ("reject", f"secret ({secret})")
    try:
        canonical = validate_config_write(key, value)
    except ConfigWriteError as exc:
        return ("reject", f"invalid: {exc}")
    return ("skip", canonical) if canonical == effective_default(key) else ("write", canonical)


def import_toml_to_db(
    text: str,
    *,
    dry_run: bool = False,
    scan_terms: tuple[str, ...] | None = None,
) -> ConfigImport:
    """Load a ``config_setting export`` TOML dump into the ``ConfigSetting`` store — the export inverse.

    Retired aliases fold onto their live key; unknown keys and secret/personal-identifier
    rows are REJECTED (the whole import is refused if any row is rejected, so a bad key never
    leaves a partial store); every value is validated through the same registry parser the
    resolver applies on read. A value equal to the shipped default writes NO row (the #3676
    zero-seed + ``restore = delete row`` property), so a dump of ``defaults.toml`` imports to
    zero rows. ``dry_run`` classifies without writing. Raises ``tomllib.TOMLDecodeError`` on
    malformed input.
    """
    doc = tomllib.loads(text)
    terms = scan_terms if scan_terms is not None else _resolve_export_scan_terms()
    to_write: list[ImportedRow] = []
    skipped: list[ImportedRow] = []
    folded: list[tuple[str, str]] = []
    rejected: list[RejectedRow] = []
    for scope, raw_key, value in _import_candidates(doc):
        key = RENAMED_SETTING_KEYS.get(raw_key, raw_key)
        if key != raw_key:
            folded.append((raw_key, key))
        kind, payload = _classify_import_row(key, value, terms)
        if kind == "reject":
            rejected.append(RejectedRow(scope, key, str(payload)))
        elif kind == "skip":
            skipped.append(ImportedRow(scope, key, payload))
        else:
            to_write.append(ImportedRow(scope, key, payload))
    if rejected:
        return ConfigImport((), tuple(skipped), tuple(folded), tuple(rejected), dry_run)
    if not dry_run:
        for row in to_write:
            ConfigSetting.objects.set_value(row.key, row.value, scope=row.scope)
    return ConfigImport(tuple(to_write), tuple(skipped), tuple(folded), (), dry_run)
