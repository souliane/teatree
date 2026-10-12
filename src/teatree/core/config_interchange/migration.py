"""``ConfigSetting`` store + seed rows -> TOML export, and its inverse.

``config_setting export`` dumps the DB config store to TOML text (stdout or a
file) so an operator has a human-readable, re-importable backup of their private
config. It is NOT the config home — teatree reads config only from the DB — it is
a one-way dump for backup/inspection. The secret guard withholds customer/brand
rows from a SHARED export by default (``SECRET_SETTINGS`` + a live banned-term
content scan); ``--include-private`` selects a personal backup; suppressed terms need a second flag.

The shipped defaults an operator tunes are not only ``ConfigSetting`` keys: the loops,
modes and schedules are too, so the same command carries them. They ride the SAME
override rule as a setting — a ``ConfigSetting`` row exists only where a value moved off
its default, so the ``[loops]`` / ``[modes]`` / ``[schedules]`` tables carry only the
fields a live row was tuned away from its ``defaults.toml`` seed. An untouched box
exports none of them, and re-importing ``defaults.toml`` itself writes nothing.
"""

import tomllib
from typing import Any

import tomlkit
from django.db import transaction

from teatree.config.cold_defaults import DEFAULTS_TOML, shipped_defaults_table
from teatree.config.defaults_snapshot import default_category_keys
from teatree.config.defaults_snapshot import render_toml as render_shipped_file
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.provenance import ValueSource, resolve_settings
from teatree.config.registries import REGISTRY_KEYS
from teatree.config.setting_groups import grouped_settings_table
from teatree.core.config_interchange.document_layout import (
    GLOBAL_SCOPE,
    OVERLAYS_TABLE,
    TABLE_REGISTRY_KEYS,
    registry_value,
    sorted_table,
)
from teatree.core.config_interchange.export_rows import ExportGuard, _configuration_rows, _exportable_rows
from teatree.core.config_interchange.import_rows import ImportPolicy, ImportRows, classify_document, seed_row
from teatree.core.config_interchange.secret_guard import (
    ExportPrivacy,
    RedactedRow,
    ScanTermsUnavailableError,
    is_private_backup,
    mark_private_backup,
    resolve_export_scan_terms,
    stamp_private_backup,
)
from teatree.core.config_interchange.seed_tables import emit_seed_tables, write_seed_field
from teatree.core.config_interchange.types import ConfigExport, ConfigImport, ImportedRow, OmittedRow
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import ConfigValue

#: Why an unshipped seed entry is left out of every export, so the file still restores.
UNSHIPPED_SEED_REASON = "not shipped — import has no home for it"


def export_db_to_toml(
    overlay: str | None = None,
    *,
    privacy: ExportPrivacy = ExportPrivacy.SHARED,
    scan_terms: tuple[str, ...] | None = None,
    default_keys_only: bool = False,
    include_defaults: bool = False,
) -> ConfigExport:
    """Serialise the ``ConfigSetting`` store to TOML — a personal, re-importable backup.

    Global-scope settings render under ``[teatree]``; each overlay renders under
    ``[overlays.<name>]`` (its registry DEFINITIONS merged with its per-overlay SETTING
    scope rows); every other registry key renders as its own ``[<key>.<name>]`` tables. No
    registry key is EVER dumped under ``[teatree]`` (they are not ``UserSettings`` fields).
    With *overlay* the dump is scoped to that one overlay's ``[overlays.<name>]`` table;
    omitted, it dumps the global scope plus every overlay scope plus every other registry.

    Two INDEPENDENT filters widen the dump, both off by default so an unfiltered call
    emits exactly the rows it always has (the nesting below re-shapes how they RENDER,
    on every surface, but adds and drops nothing):

    *   *default_keys_only* restricts what is ELIGIBLE to the ``Category.DEFAULT`` key set
        the shipped file carries — dropping registries, secrets, personal identifiers and
        every ``[overlays.<name>]`` scope;
    *   *include_defaults* widens WHICH of the eligible keys are emitted from
        divergent-only (a DB row exists) to all of them, filling a key with no row from
        :func:`~teatree.config.provenance.resolve_settings`.

    Ticking BOTH is the defaults shape: the emitted key set is exactly the
    ``Category.DEFAULT`` set, which is what makes ``export(import(defaults.toml))``
    reproduce ``defaults.toml`` byte for byte. That combination renders through
    :func:`~teatree.config.defaults_snapshot.render_toml`, the same emitter the
    owner-approved ``snapshot_settings_defaults`` writes with, so the shipped file and the
    exported file can never come from two writers.

    By DEFAULT (``privacy`` SHARED) the secret guard withholds any row that is a
    known-private key (``SECRET_SETTINGS``) OR whose key/value contains a banned
    customer/brand term (``scan_terms``, resolved from the live config when not supplied) —
    so a SHARED export cannot leak customer data even though the private DB store keeps it.
    PRIVATE is a personal backup: it carries the key classes but still withholds every row
    carrying a banned term, the term registry itself included. Only
    ``PRIVATE_WITH_TERM_VALUES`` carries those, and it resolves no terms at all. Everything
    under a scope or entry NAME carrying a term is withheld too, reported without the name.
    The withheld rows ride back on the result so the caller can warn what it dropped.

    The two scanning shapes raise :class:`ScanTermsUnavailableError` when the store holds no
    ban-class term: an unclassified row would leave the box.

    A stored row that is not CONFIGURATION at all — internal runtime state sharing the
    store, a key outliving its declaration — is omitted whatever the filters say, and
    rides back the same way (#4147), as does a loop, preset or schedule the shipped file
    does not carry. Not a privacy rule but an interchange one: the import has no home for
    either and refuses the whole file on one.

    Every private dump STAMPS itself a personal backup (#4156), defaults shape included, so
    the file says which of the two formats it is and ``import --restore-private`` can read
    it back. The SHARED defaults shape stays byte-identical to ``defaults.toml``.
    """
    terms = () if privacy is ExportPrivacy.PRIVATE_WITH_TERM_VALUES else scan_terms
    guard = ExportGuard(privacy, resolve_export_scan_terms() if terms is None else terms, redacted=[], omitted=[])
    document = tomlkit.document()
    if guard.private_backup:
        mark_private_backup(document)
    all_global = ConfigSetting.objects.overrides_for_scope(GLOBAL_SCOPE)
    overlays_registry = registry_value(all_global, OVERLAYS_TABLE)

    if overlay is not None:
        scoped_registry = {overlay: overlays_registry[overlay]} if overlay in overlays_registry else {}
        _emit_overlay_tables(document, [overlay], scoped_registry, guard=guard)
        return _exported(tomlkit.dumps(document), guard)

    # The registry keys are rendered as their own top-level tables below, never under
    # ``[teatree]`` (they are NOT ``UserSettings`` fields) — exclude them from the
    # global settings table so the dump re-imports cleanly.
    stored_global = {key: value for key, value in all_global.items() if key not in REGISTRY_KEYS}
    settings_global = _configuration_rows(stored_global, GLOBAL_SCOPE, guard=guard)
    if default_keys_only:
        settings_global = {key: value for key, value in settings_global.items() if key in default_category_keys()}
    global_rows = _exportable_rows(settings_global, GLOBAL_SCOPE, guard=guard)
    if include_defaults:
        global_rows = _filled_with_defaults(global_rows, default_keys_only=default_keys_only, guard=guard)
    if default_keys_only and include_defaults:
        text = render_shipped_file(global_rows, base_text=_shipped_file_text())
        return _exported(stamp_private_backup(text) if guard.private_backup else text, guard)
    if global_rows:
        document["teatree"] = grouped_settings_table(global_rows)
    if not default_keys_only:
        scopes = list(
            ConfigSetting.objects.exclude(scope=GLOBAL_SCOPE)
            .order_by("scope")
            .values_list("scope", flat=True)
            .distinct()
        )
        _emit_overlay_tables(document, scopes, overlays_registry, guard=guard)
        for key in TABLE_REGISTRY_KEYS:
            _emit_registry_tables(document, key, registry_value(all_global, key), guard=guard)
    _emit_seed_tables(document, guard=guard)
    return _exported(tomlkit.dumps(document), guard)


def _exported(text: str, guard: ExportGuard) -> ConfigExport:
    return ConfigExport(text, tuple(guard.redacted), tuple(guard.omitted), private_backup=guard.private_backup)


def _emit_seed_tables(document: tomlkit.TOMLDocument, *, guard: ExportGuard) -> None:
    """The seed half, under the same guard — a withheld field rides back as a `RedactedRow`."""
    emission = emit_seed_tables(document, sorted_table, guard=guard)
    guard.redacted.extend(RedactedRow(one.scope, one.field, one.reason) for one in emission.withheld)
    guard.omitted.extend(OmittedRow(GLOBAL_SCOPE, entry, UNSHIPPED_SEED_REASON) for entry in emission.unshipped)


def _shipped_file_text() -> str:
    """The committed ``defaults.toml`` as the base a defaults-shape export rewrites.

    Every hand-written comment and every seed table rides through untouched, so the dump
    is a drop-in replacement for the file rather than a fragment of one. A missing file
    (never true in a checkout) leaves the emitter to build the document header included.
    """
    try:
        return DEFAULTS_TOML.read_text(encoding="utf-8")
    except OSError:
        return ""


def _filled_with_defaults(
    rows: dict[str, ConfigValue], *, default_keys_only: bool, guard: ExportGuard
) -> dict[str, ConfigValue]:
    """Add every eligible key that has no DB row, at its resolved effective value.

    Eligibility is filter 1's question, answered before this one: the ``Category.DEFAULT``
    set, or every ``[teatree]``-emittable key. Resolution is
    :func:`~teatree.config.provenance.resolve_settings` restricted to the persisted tiers —
    ``env`` and the active overlay's code defaults are this machine's state, not the file's.

    A key the shipped file does not carry is left out when only its DECLARED default
    reaches it: a Personal/Secret key's declaration is a machine path or an empty
    credential the ``[teatree]`` table has never held, and several are not even in stored
    form (a ``Path``, an enum). ``default_category_keys()`` IS that carried set, so the
    defaults shape stays exhaustive while the wider shape gains nothing it never emitted.

    A filled key the secret guard would withhold falls back to the value the shipped file
    already ships in public, rather than leaving a hole: the defaults shape is only
    meaningful when it is COMPLETE, and the shipped value is public by construction. A key
    the guard ALREADY withheld on the way in is filled the same way but not reported twice.
    """
    carried = default_category_keys()
    eligible = carried if default_keys_only else _teatree_table_keys()
    shipped = shipped_defaults_table()
    withheld = {row.key for row in guard.redacted if row.scope == GLOBAL_SCOPE}
    filled = dict(rows)
    for key, entry in resolve_settings(sorted(eligible - set(rows)), persisted_only=True).items():
        if entry.source is ValueSource.CODE_DEFAULT and key not in carried:
            continue
        reason = guard.reason(key, entry.value)
        if reason is None:
            filled[key] = entry.value
            continue
        if key not in withheld:
            guard.redacted.append(RedactedRow(GLOBAL_SCOPE, key, reason))
        if key in shipped:
            filled[key] = shipped[key]
    return filled


def _teatree_table_keys() -> frozenset[str]:
    """Every known setting the ``[teatree]`` table may carry — the registries excepted."""
    return frozenset(ALL_KNOWN_CONFIG_SETTINGS) - frozenset(REGISTRY_KEYS)


def _emit_overlay_tables(
    document: tomlkit.TOMLDocument,
    scopes: list[str],
    overlays_registry: dict[str, Any],
    *,
    guard: ExportGuard,
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
        scope_rows = ConfigSetting.objects.overrides_for_scope(name)
        definitions = overlays_registry.get(name, {})
        if guard.withheld_name(name, len({**definitions, **scope_rows})):
            continue
        stored = _configuration_rows(scope_rows, name, guard=guard)
        rows = _exportable_rows({**definitions, **stored}, name, guard=guard)
        if rows:
            overlays[name] = sorted_table(rows)
            emitted = True
    if emitted:
        document["overlays"] = overlays


def _emit_registry_tables(
    document: tomlkit.TOMLDocument,
    key: str,
    registry: dict[str, Any],
    *,
    guard: ExportGuard,
) -> None:
    """Attach a ``[<key>.<name>]`` sub-table per entry of the *key* registry.

    The inverse of a registry loader reading ``raw[key]`` — each entry's own fields
    rendered as its own table. The super-table is added only when an entry has rows
    surviving the secret guard.
    """
    entries = tomlkit.table(is_super_table=True)
    emitted = False
    for name in sorted(registry):
        entry = registry[name]
        if not isinstance(entry, dict) or guard.withheld_name(name, len(entry)):
            continue
        rows = _exportable_rows(entry, f"{key}.{name}", guard=guard)
        if rows:
            entries[name] = sorted_table(rows)
            emitted = True
    if emitted:
        document[key] = entries


# ---- import (the inverse of export) -----------------------------------------------------


def import_toml_to_db(
    text: str,
    *,
    dry_run: bool = False,
    scan_terms: tuple[str, ...] | None = None,
    allow_safety_posture: bool = False,
    restore_private: bool = False,
) -> ConfigImport:
    """Load a ``config_setting export`` TOML dump into the ``ConfigSetting`` store — the export inverse.

    Unknown keys and secret/personal-identifier rows are REJECTED (the whole
    import is refused if any row is rejected, so a bad key never
    leaves a partial store); every value is validated through the same registry parser the
    resolver applies on read. A value equal to the shipped default writes NO row (the #3676
    zero-seed + ``restore = delete row`` property), so a dump of ``defaults.toml`` imports to
    zero rows. A row the store ALREADY holds at that value is ``unchanged`` rather than a write.

    What the export could NOT carry cannot become a change either, which is what makes the round
    trip a genuine no-op rather than an almost-no-op: a row omitted as non-configuration is
    simply absent from the file, and a registry field the secret guard withheld is merged back
    from the store (:mod:`teatree.core.config_interchange.registry_rows`) rather than deleted. An import
    writes values and never removes one; removing a value is ``config_setting clear``.

    ``dry_run`` classifies without writing. Raises ``tomllib.TOMLDecodeError`` on malformed
    input.

    The seed tables follow the same contract onto their own rows: each entry is classified
    against what ``defaults.toml`` ships, so an entry equal to the seed writes nothing while
    an unknown entry, an unknown field or a wrong-typed value refuses the whole import. The
    zero-write property therefore holds because every seed entry is UNDERSTOOD, not because
    the tables are ignored.

    ``allow_safety_posture`` declares that the operator authorized the safety-posture keys in
    *text* — the dashboard passes it only when the typed confirm phrase is present, the CLI
    passes it because a directly-typed ``config_setting import`` IS that authorization. It
    defaults to False so a caller that never considered the question refuses those keys.
    Each written row carries ``is_safety_posture`` so a dry-run preview can flag them.

    ``restore_private`` accepts the private rows an ``--include-private`` backup carries, so
    the flag whose purpose is a COMPLETE backup produces a file that can actually be restored
    (#4156). It grants nothing on its own: the allowance also needs *text* to declare itself a
    personal backup, so an ordinary shared dump is refused under it exactly as without it, and
    the default-False posture every other caller keeps is untouched. Each such row carries
    ``is_private`` so every render site withholds its VALUE while still naming the key — the
    flag is what lets a private row reach a renderer at all.

    A store with no ban-class term cannot content-scan anything, and the receiving box is what
    the scan protects, so the import proceeds UNSCANNED rather than refusing: the key classes
    still apply, every value is shown withheld, and ``unscanned`` tells the caller to say so.
    Rows under a scope NAME carrying a term are refused without the name, or restored with
    the name withheld from every report under ``restore_private``.
    """
    doc = tomllib.loads(text)
    policy = ImportPolicy(
        terms=scan_terms if scan_terms is not None else _available_scan_terms(),
        allow_safety_posture=allow_safety_posture,
        private_backup=is_private_backup(doc),
        restore_private=restore_private,
    )
    rows = classify_document(doc, policy)
    if rows.rejected:
        return _imported(policy, rows, written=(), dry_run=dry_run)
    if not dry_run:
        with transaction.atomic():
            ConfigSetting.objects.set_values([(row.key, row.value, row.scope) for row in rows.to_write])
            for entry in rows.seed_writes:
                write_seed_field(entry.table, entry.name, entry.field, entry.value)
    written = (*rows.to_write, *(seed_row(entry, policy) for entry in rows.seed_writes))
    return _imported(policy, rows, written=written, dry_run=dry_run)


def _imported(
    policy: ImportPolicy, rows: ImportRows, *, written: tuple[ImportedRow, ...], dry_run: bool
) -> ConfigImport:
    return ConfigImport(
        written,
        tuple(rows.skipped),
        tuple(rows.rejected),
        dry_run,
        tuple(rows.unchanged),
        private_backup=policy.private_backup,
        unscanned=policy.unscanned,
    )


def _available_scan_terms() -> tuple[str, ...] | None:
    try:
        return resolve_export_scan_terms()
    except ScanTermsUnavailableError:
        return None
