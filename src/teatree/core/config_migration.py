"""TOML -> ``ConfigSetting`` import service — the #938 dual-read migration (TODO-75).

The reusable seam that seeds the DB override store from the existing
``~/.teatree.toml`` so the cutover to the #1775 DB/TOML hard partition does not
lose a user's pre-partition config. Both callers share it:

*   ``t3 <overlay> config_setting import`` — the manual one-time migration, with
    its original CLOBBER semantics (re-import refreshes every operational key
    from the file, overwriting any stale DB row).
*   ``t3 setup`` — the AUTOMATIC migration. It runs on every update, so it calls
    with ``clobber=False``: it seeds only keys ABSENT from the store and never
    overwrites a value the user has since changed via ``config_setting set``.

Every ``[teatree]`` key that is a registered ``OVERLAY_OVERRIDABLE_SETTINGS``
(= DB-home) field OR a pre-Django ``COLD_HOOK_SETTINGS`` key (config-unify PR2)
is coerced through its parser and upserted into the GLOBAL scope; every operational
key under an ``[overlays.<name>]`` table is upserted into THAT overlay's scope —
the DB twin of the per-overlay TOML override (#1775). The cold-hook keys are
GLOBAL-only, so the per-overlay walk consults only the overridable registry.
Bootstrap-file-only keys (``private_repos`` / ``DATABASE_URL`` / …), the overlay's
own ``path`` / ``url`` discovery keys, and unknown keys are skipped: only
operational + cold-hook settings move.

The service returns a structured :class:`ConfigImportResult` rather than writing
to a stream, so the management command renders it and ``t3 setup`` logs a one-line
summary from the same outcome.
"""

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config import COLD_HOOK_SETTINGS, OVERLAY_OVERRIDABLE_SETTINGS
from teatree.config.registries import REGISTRY_KEYS, REGISTRY_SETTINGS
from teatree.config.secret_settings import SECRET_SETTINGS
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import ConfigValue
from teatree.hooks.term_match import matched_term

GLOBAL_SCOPE = ""


def _global_parsers() -> dict[str, Callable[[Any], Any]]:
    """Parsers recognised in the global ``[teatree]`` table.

    The DB-home overridable registry UNION the pre-Django cold-hook keys
    (config-unify PR2). The cold-hook keys are GLOBAL-only — they are deliberately
    left out of the per-overlay registry below, so an ``[overlays.<name>]`` gate
    flag is never mis-scoped to an overlay row the cold reader would not consult.
    """
    return {**OVERLAY_OVERRIDABLE_SETTINGS, **{key: setting.parse for key, setting in COLD_HOOK_SETTINGS.items()}}


def _scope_label(scope: str) -> str:
    """Human label for a scope: ``global`` for the empty scope else ``overlay '<name>'``."""
    return "global" if not scope else f"overlay {scope!r}"


@dataclass
class ConfigImportResult:
    """Outcome of a TOML -> ``ConfigSetting`` import pass.

    ``imported`` counts rows newly written (a key with no prior row in its
    scope). ``overwritten`` counts existing rows replaced (clobber mode only).
    ``preserved`` counts rows left untouched because a value already existed and
    the caller asked not to clobber. ``skipped`` counts file keys that are not
    operational DB-home settings (bootstrap / discovery / unknown / invalid).
    ``rows`` records ``(scope, key)`` for every row written or overwritten so the
    summary can name what moved; ``skipped_reasons`` records the loud-skip lines
    for invalid values.
    """

    imported: int = 0
    overwritten: int = 0
    preserved: int = 0
    skipped: int = 0
    rows: list[tuple[str, str]] = field(default_factory=list)
    skipped_reasons: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        """Rows actually written this pass (new + overwritten)."""
        return self.imported + self.overwritten

    def summary(self) -> str:
        """One-line human summary naming the count and the scopes touched."""
        if not self.rows:
            return f"imported {self.changed} setting(s) into the DB store"
        scopes = sorted({_scope_label(scope) for scope, _ in self.rows})
        return f"imported {self.changed} setting(s) into the DB store [{', '.join(scopes)}]"


def import_toml_into_db(raw: dict, *, clobber: bool = True) -> ConfigImportResult:
    """Seed the ``ConfigSetting`` store from a raw config dict; return the outcome.

    Walks the global ``[teatree]`` table into the global scope and each
    ``[overlays.<name>]`` table into that overlay's scope, plus the top-level
    ``[mr_reminder]`` setting and the ``[overlays]`` / ``[e2e_repos]`` registry
    tables (each stored whole as one global row). With *clobber* (the default, the
    manual ``config_setting import`` semantics) an existing row is overwritten from
    the file value. With ``clobber=False`` (the ``t3 setup`` auto-migration) a key
    that already has a row in its scope is left untouched and counted as
    ``preserved`` — so a value the user changed via ``config_setting set`` survives
    every later ``t3 setup``.
    """
    result = ConfigImportResult()
    teatree_table = raw.get("teatree")
    if isinstance(teatree_table, dict):
        _import_table(teatree_table, GLOBAL_SCOPE, clobber=clobber, result=result, parsers=_global_parsers())
    # ``[mr_reminder]`` is a TOP-LEVEL table (not under ``[teatree]``), but the whole
    # table IS the value of the single DB-home ``mr_reminder`` setting — seed it into
    # the global scope through the same per-key machinery so a migrating install
    # moves it without a manual ``config_setting set`` (eliminate-~/.teatree.toml).
    mr_reminder_table = raw.get("mr_reminder")
    if isinstance(mr_reminder_table, dict):
        _import_table(
            {"mr_reminder": mr_reminder_table},
            GLOBAL_SCOPE,
            clobber=clobber,
            result=result,
            parsers={"mr_reminder": OVERLAY_OVERRIDABLE_SETTINGS["mr_reminder"]},
        )
    overlays = raw.get("overlays")
    if isinstance(overlays, dict):
        for overlay_name, overlay_cfg in overlays.items():
            if isinstance(overlay_cfg, dict):
                _import_table(
                    overlay_cfg, overlay_name, clobber=clobber, result=result, parsers=OVERLAY_OVERRIDABLE_SETTINGS
                )
    _import_registry_tables(raw, clobber=clobber, result=result)
    return result


def _overlay_definition_registry(overlays_table: dict) -> dict[str, Any]:
    """The overlay DEFINITION keys (``path`` / ``class`` / ...) per overlay name.

    The payload of the ``overlays`` registry row: each ``[overlays.<name>]`` table with
    every recognised SETTING key STRIPPED OUT — both the per-overlay overridable settings
    (migrated into that overlay's own scope rows by the per-overlay loop) and the
    global-only cold-hook keys (which the per-overlay walk ignores). So the registry
    carries only what ``discover_overlays`` needs (the overlay list + each one's
    ``path`` / ``class``), never a setting. An overlay table that is pure setting-overrides
    (no definition key) contributes no entry: its overlay is defined by an entry point,
    not the registry.
    """
    setting_keys = set(_global_parsers())
    registry: dict[str, Any] = {}
    for name, cfg in overlays_table.items():
        if not isinstance(cfg, dict):
            continue
        definitions = {key: value for key, value in cfg.items() if key not in setting_keys}
        if definitions:
            registry[name] = definitions
    return registry


def _import_registry_tables(raw: dict, *, clobber: bool, result: ConfigImportResult) -> None:
    """Seed the DB-home ``overlays`` / ``e2e_repos`` registries (eliminate-~/.teatree.toml).

    The two NON-``UserSettings`` config tables (their readers consult ``config.raw``
    directly, see ``config.registries``) move whole into ONE global row each so a migrating
    install carries its overlay definitions + e2e repos in the DB and boots with no toml.
    ``overlays`` is reduced to its DEFINITION keys (the SETTING overrides go to the
    per-overlay scope rows above — keeping the two orthogonal makes ``export -> import``
    a fixed point); ``e2e_repos`` (no setting overload) moves verbatim.
    """
    overlays = raw.get("overlays")
    overlays_registry = _overlay_definition_registry(overlays) if isinstance(overlays, dict) else {}
    if overlays_registry:
        _import_table(
            {"overlays": overlays_registry},
            GLOBAL_SCOPE,
            clobber=clobber,
            result=result,
            parsers={"overlays": REGISTRY_SETTINGS["overlays"]},
        )
    e2e_repos = raw.get("e2e_repos")
    if isinstance(e2e_repos, dict):
        _import_table(
            {"e2e_repos": e2e_repos},
            GLOBAL_SCOPE,
            clobber=clobber,
            result=result,
            parsers={"e2e_repos": REGISTRY_SETTINGS["e2e_repos"]},
        )


@dataclass(frozen=True)
class RedactedRow:
    """One export row withheld by the secret guard, with the reason it was dropped."""

    scope: str
    key: str
    reason: str  # "private-key" or "banned-term:<term>"


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
    """Banned terms + brands for the export content scan; fails safe to empty."""
    from teatree.hooks.banned_terms_cli import resolve_banned_terms  # noqa: PLC0415
    from teatree.hooks.banned_terms_scanner import resolve_config  # noqa: PLC0415
    from teatree.hooks.banned_terms_tree_scan import BannedTermsUnsetError, load_brand_terms  # noqa: PLC0415

    config = resolve_config()
    if config is None:
        return ()
    # Each source fails safe to empty INDEPENDENTLY: a config present but with
    # banned_terms (or banned_brands) genuinely unset raises BannedTermsUnsetError,
    # and the export must never crash on it (the DEFAULT machine state). Two
    # separate suppress blocks so an unset banned_terms list does not also drop the
    # brand-term redaction (and vice versa) — a shared export still scans for
    # whichever list IS configured.
    terms: list[str] = []
    with contextlib.suppress(BannedTermsUnsetError):
        terms.extend(resolve_banned_terms(config))
    with contextlib.suppress(BannedTermsUnsetError):
        terms.extend(load_brand_terms(config))
    return tuple(terms)


def _redaction_reason(key: str, value: ConfigValue, terms: tuple[str, ...]) -> str | None:
    """Why this row must not be shared (private key, or a value carrying a banned term), else None."""
    if key in SECRET_SETTINGS:
        return "private-key"
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
    """Serialise the ``ConfigSetting`` store back to TOML — the inverse of import.

    Global-scope settings render under ``[teatree]``; each overlay renders under
    ``[overlays.<name>]`` (its registry DEFINITIONS merged with its per-overlay SETTING
    scope rows); the ``e2e_repos`` registry renders as ``[e2e_repos.<name>]`` tables. The
    two registry keys are NEVER dumped under ``[teatree]`` (they are not ``UserSettings``
    fields). Each stored value is emitted as its native TOML scalar so
    ``export -> import -> export`` is a fixed point. With *overlay* the dump is scoped to
    that one overlay's ``[overlays.<name>]`` table; omitted, it dumps the global scope plus
    every overlay scope plus the e2e-repos registry.

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
    (its scope rows). Re-importing splits them back apart (settings to scope rows,
    definitions to the registry), so the dump is an ``export -> import`` fixed point.
    The names are the registry overlays UNION the setting scopes, deduped order-stable.
    The ``overlays`` super-table is added only when at least one overlay has rows that
    survive the secret guard, so an empty store stays an empty document rather than a
    bare ``[overlays]`` header.
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
    ``url`` / ``branch`` / ``e2e_dir`` rendered as its own table so the dump re-imports
    into the ``e2e_repos`` registry row. The super-table is added only when a repo has
    rows surviving the secret guard.
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


def _import_table(
    table: dict,
    scope: str,
    *,
    clobber: bool,
    result: ConfigImportResult,
    parsers: dict[str, Callable[[Any], Any]],
) -> None:
    """Upsert every operational key in *table* into *scope*, mutating *result*.

    A key present in *parsers* is coerced through its parser and (per *clobber*)
    written or preserved; every other key is skipped. *parsers* is the DB-home
    overridable registry for an overlay table, unioned with the global-only
    cold-hook keys for the ``[teatree]`` table (config-unify PR2). An invalid
    value is recorded loud and skipped — never fatal — so one bad key cannot abort
    the migration of the rest.
    """
    for key, raw_value in table.items():
        parser = parsers.get(key)
        if parser is None:
            result.skipped += 1
            continue
        try:
            canonical = parser(raw_value)
        except (ValueError, TypeError, AttributeError) as exc:
            result.skipped += 1
            result.skipped_reasons.append(
                f"skipped {key!r} [{_scope_label(scope)}]: invalid value {raw_value!r}: {exc}"
            )
            continue
        existed = ConfigSetting.objects.get_effective(key, scope=scope) is not None
        if existed and not clobber:
            result.preserved += 1
            continue
        ConfigSetting.objects.set_value(key, canonical, scope=scope)
        result.rows.append((scope, key))
        if existed:
            result.overwritten += 1
        else:
            result.imported += 1
