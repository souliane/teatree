"""``ConfigSetting`` store + seed rows -> TOML export, and its inverse.

``config_setting export`` dumps the DB config store to TOML text (stdout or a
file) so an operator has a human-readable, re-importable backup of their private
config. It is NOT the config home — teatree reads config only from the DB — it is
a one-way dump for backup/inspection. The secret guard withholds customer/brand
rows from a SHARED export by default (``SECRET_SETTINGS`` + a live banned-term
content scan); ``--include-private`` exports everything for a personal backup.

The shipped defaults an operator tunes are not only ``ConfigSetting`` keys: the loops,
modes and schedules are too, so the same command carries them. They ride the SAME
override rule as a setting — a ``ConfigSetting`` row exists only where a value moved off
its default, so the ``[loops]`` / ``[modes]`` / ``[schedules]`` tables carry only the
fields a live row was tuned away from its ``defaults.toml`` seed. An untouched box
exports none of them, and re-importing ``defaults.toml`` itself writes nothing.
"""

import tomllib
from dataclasses import dataclass
from typing import Any

import tomlkit
from tomlkit import items as tomlkit_items

from teatree.config import effective_default
from teatree.config.cold_defaults import DEFAULTS_TOML, flatten_settings_table, shipped_defaults_table
from teatree.config.defaults_snapshot import default_category_keys
from teatree.config.defaults_snapshot import render_toml as render_shipped_file
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.provenance import PERSISTED_SOURCES, resolve_settings
from teatree.config.registries import REGISTRY_KEYS
from teatree.config.retired_settings import REMOVED_SETTING_KEYS, RENAMED_SETTING_KEYS, removed_setting
from teatree.config.setting_groups import grouped_settings_table
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.config.stored_row_health import is_operator_configuration, stored_row_kind
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.config_interchange.registry_rows import merged_registry, overlay_table_split
from teatree.core.config_interchange.secret_guard import RedactedRow, redaction_reason, resolve_export_scan_terms
from teatree.core.config_interchange.seed_tables import (
    SeedFieldDisposition,
    classify_seed_rows,
    emit_seed_tables,
    holds_value,
    unseeded_entries,
    write_seed_field,
)
from teatree.core.config_interchange.types import ConfigExport, ConfigImport, ImportedRow, OmittedRow, RejectedRow
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import ConfigValue

GLOBAL_SCOPE = ""
_TEATREE_TABLE = "teatree"
_OVERLAYS_TABLE = "overlays"
_E2E_REPOS_TABLE = "e2e_repos"


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
    omitted: list[OmittedRow]


def _configuration_rows(rows: dict[str, ConfigValue], scope: str, *, guard: _ExportGuard) -> dict[str, ConfigValue]:
    """Drop rows that are not configuration, recording each in ``guard.omitted`` (#4147).

    The ``ConfigSetting`` store also holds internal runtime state and rows outliving the
    key they were written under. They are not settings — the registry refuses to ``get``
    them — so the import has no home for them and refuses the WHOLE file on one, which
    left a live box unable to re-import its own export. Export and import are inverses or
    they are neither, and the safety posture worth keeping is the import's all-or-nothing
    refusal; so the export is the side that stops offering a key nothing can read back.

    Applied to SETTING rows only. An overlay's definition keys (``path`` / ``class``) and
    an e2e repo's fields are not settings and are not classified here.
    """
    kept: dict[str, ConfigValue] = {}
    for key, value in rows.items():
        if is_operator_configuration(key):
            kept[key] = value
        else:
            guard.omitted.append(OmittedRow(scope, key, stored_row_kind(key)))
    return kept


def _exportable_rows(rows: dict[str, ConfigValue], scope: str, *, guard: _ExportGuard) -> dict[str, ConfigValue]:
    """Drop secret/tainted rows (recording each in ``guard.redacted``) unless include_private."""
    if guard.include_private:
        return rows
    kept: dict[str, ConfigValue] = {}
    for key, value in rows.items():
        reason = redaction_reason(key, value, guard.terms)
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
    default_keys_only: bool = False,
    include_defaults: bool = False,
) -> ConfigExport:
    """Serialise the ``ConfigSetting`` store to TOML — a personal, re-importable backup.

    Global-scope settings render under ``[teatree]``; each overlay renders under
    ``[overlays.<name>]`` (its registry DEFINITIONS merged with its per-overlay SETTING
    scope rows); the ``e2e_repos`` registry renders as ``[e2e_repos.<name>]`` tables. The
    two registry keys are NEVER dumped under ``[teatree]`` (they are not ``UserSettings``
    fields). With *overlay* the dump is scoped to that one overlay's ``[overlays.<name>]``
    table; omitted, it dumps the global scope plus every overlay scope plus the e2e-repos
    registry.

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

    By DEFAULT the secret guard withholds any row that is a known-private key
    (``SECRET_SETTINGS``) OR whose key/value contains a banned customer/brand term
    (``scan_terms``, resolved from the live config when not supplied) — so a SHARED
    export cannot leak customer data even though the private DB store keeps it.
    ``include_private`` exports everything for a personal, never-shared backup. The
    withheld rows ride back on the result so the caller can warn what it dropped.

    A stored row that is not CONFIGURATION at all — internal runtime state sharing the
    store, a key outliving its declaration — is omitted whatever the filters say, and
    rides back the same way (#4147). Not a privacy rule but an interchange one: the
    import has no home for such a key and refuses the whole file on it.
    """
    terms = scan_terms if scan_terms is not None else resolve_export_scan_terms()
    guard = _ExportGuard(include_private=include_private, terms=terms, redacted=[], omitted=[])
    document = tomlkit.document()
    all_global = ConfigSetting.objects.overrides_for_scope(GLOBAL_SCOPE)
    overlays_registry = _registry_value(all_global, "overlays")
    e2e_repos_registry = _registry_value(all_global, "e2e_repos")

    if overlay is not None:
        scoped_registry = {overlay: overlays_registry[overlay]} if overlay in overlays_registry else {}
        _emit_overlay_tables(document, [overlay], scoped_registry, guard=guard)
        return ConfigExport(tomlkit.dumps(document), tuple(guard.redacted), tuple(guard.omitted))

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
        return ConfigExport(
            render_shipped_file(global_rows, base_text=_shipped_file_text()),
            tuple(guard.redacted),
            tuple(guard.omitted),
        )
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
        _emit_e2e_repos_tables(document, e2e_repos_registry, guard=guard)
    emit_seed_tables(document, _toml_table)
    return ConfigExport(tomlkit.dumps(document), tuple(guard.redacted), tuple(guard.omitted))


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
    rows: dict[str, ConfigValue], *, default_keys_only: bool, guard: _ExportGuard
) -> dict[str, ConfigValue]:
    """Add every eligible key that has no DB row, at its resolved effective value.

    Eligibility is filter 1's question, answered before this one: the ``Category.DEFAULT``
    set, or every ``[teatree]``-emittable key. Resolution is
    :func:`~teatree.config.provenance.resolve_settings` restricted to the persisted tiers —
    ``env`` and the active overlay's code defaults are this machine's state, not the file's.

    A key no persisted tier reaches is left out: its only value is an in-code dataclass
    default, which is not in stored form (a ``Path``, an enum) and has never been part of
    the ``[teatree]`` table. Every ``Category.DEFAULT`` key IS in the shipped file, so the
    defaults shape stays exhaustive.

    A filled key the secret guard would withhold falls back to the value the shipped file
    already ships in public, rather than leaving a hole: the defaults shape is only
    meaningful when it is COMPLETE, and the shipped value is public by construction. A key
    the guard ALREADY withheld on the way in is filled the same way but not reported twice.
    """
    eligible = default_category_keys() if default_keys_only else _teatree_table_keys()
    shipped = shipped_defaults_table()
    withheld = {row.key for row in guard.redacted if row.scope == GLOBAL_SCOPE}
    filled = dict(rows)
    for key, entry in resolve_settings(sorted(eligible - set(rows)), persisted_only=True).items():
        if entry.source not in PERSISTED_SOURCES:
            continue
        reason = None if guard.include_private else redaction_reason(key, entry.value, guard.terms)
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
        stored = _configuration_rows(ConfigSetting.objects.overrides_for_scope(name), name, guard=guard)
        rows = _exportable_rows({**overlays_registry.get(name, {}), **stored}, name, guard=guard)
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


def _import_candidates(doc: dict[str, Any]) -> list[tuple[str, str, ConfigValue]]:
    """Flatten a parsed export document into ``(scope, key, value)`` candidate rows.

    Reverses the export layout: the ``[teatree]`` table -> global settings; each
    ``[overlays.<name>]`` table splits — through the export's own join predicate,
    :func:`~teatree.core.config_interchange.registry_rows.overlay_table_split` — into per-overlay
    SETTING rows and overlay-DEFINITION keys (``path`` / ``class`` / …, folded back into
    the ``overlays`` registry row); each ``[e2e_repos.<name>]`` table rebuilds the
    ``e2e_repos`` registry row.

    A rebuilt registry value is a candidate, not the row: it describes only what the file
    could say, and the import MERGES it onto the stored row rather than replacing it (see
    :mod:`teatree.core.config_interchange.registry_rows`).

    The ``[teatree]`` table goes through the SAME flattener the cold reader applies, so a
    nested file and a flat one import to the same rows and the group wrappers never reach
    the store as keys.
    """
    candidates: list[tuple[str, str, ConfigValue]] = []
    for key, value in flatten_settings_table(doc.get(_TEATREE_TABLE, {})).items():
        candidates.append((GLOBAL_SCOPE, key, value))
    overlays_registry: dict[str, Any] = {}
    for name, table in doc.get(_OVERLAYS_TABLE, {}).items():
        if not isinstance(table, dict):
            continue
        settings, definitions = overlay_table_split(table)
        candidates.extend((name, key, value) for key, value in settings.items())
        if definitions:
            overlays_registry[name] = definitions
    if overlays_registry:
        candidates.append((GLOBAL_SCOPE, _OVERLAYS_TABLE, overlays_registry))
    e2e_registry: dict[str, Any] = {n: dict(t) for n, t in doc.get(_E2E_REPOS_TABLE, {}).items() if isinstance(t, dict)}
    if e2e_registry:
        candidates.append((GLOBAL_SCOPE, _E2E_REPOS_TABLE, e2e_registry))
    return candidates


def _unstorable_reason(key: str, value: ConfigValue, terms: tuple[str, ...]) -> str | None:
    """Why this row has no home in the store at all, else None.

    A removed key (loud, no home), an unknown key, and a secret/personal-identifier row
    (reusing the export withhold rule so a shared TOML never smuggles customer data back in).
    """
    if key in REMOVED_SETTING_KEYS:
        entry = removed_setting(key)
        return f"removed ({entry.reason if entry is not None else 'the setting was removed'})"
    if key not in ALL_KNOWN_CONFIG_SETTINGS:
        return "unknown key"
    if (secret := redaction_reason(key, value, terms)) is not None:
        return f"secret ({secret})"
    return None


def _classify_import_row(
    key: str, value: ConfigValue, terms: tuple[str, ...], *, stored: ConfigValue | None, allow_safety_posture: bool
) -> tuple[str, ConfigValue]:
    """One row's disposition: ``("reject", reason)`` / ``("skip"|"unchanged"|"write", canonical)``.

    A storable row is coerced through the shared write-path validator; a value equal to the
    key's EFFECTIVE default (:func:`~teatree.config.effective_default` — the resolver's own
    default, the same authority the seed-skip consults) is redundant (``skip``), leaving
    ``restore = delete row`` intact. The resolver reads ``defaults.toml`` as its DEFAULTS
    tier, so every shipped value IS that effective default: importing the shipped file
    writes zero rows, and each skipped row resolves to exactly the value the file declares.

    A registry row is MERGED onto *stored* first (:func:`~teatree.core.config_interchange.
    registry_rows.merged_registry`): its value is a table of independent facts, so the file
    can describe it INCOMPLETELY — the secret guard withholds an overlay's credential
    coordinates — and taking the file's version whole would make the redaction a deletion.
    Here is where the merge belongs, because this is where what the file says meets what the
    store holds, and a merged row equal to *stored* is exactly the ``unchanged`` below.

    A value the row in *stored* already holds is ``unchanged``. Writing it would be a
    no-op on the value and a REAL edit to the row — ``set_value`` clears seed provenance,
    so re-importing a box's own export would hand every deploy-seeded row to the operator.

    A :data:`~teatree.config.setting_registries.SAFETY_POSTURE_KEYS` row that would actually
    CHANGE the store is rejected unless the caller declares the operator authorized it — the
    same boundary the settings editor's typed confirm and the MCP write-tool refusal enforce,
    so a pasted TOML dump is not a quieter route to `autonomy = "full"`. A safety-posture value
    equal to its default, or to what the store already holds, changes nothing to authorize.
    """
    if (unstorable := _unstorable_reason(key, value, terms)) is not None:
        return ("reject", unstorable)
    candidate = merged_registry(value, stored) if key in REGISTRY_KEYS and isinstance(value, dict) else value
    try:
        canonical = validate_config_write(key, candidate)
    except ConfigWriteError as exc:
        return ("reject", f"invalid: {exc}")
    if canonical == effective_default(key):
        return ("skip", canonical)
    if canonical == stored:
        return ("unchanged", canonical)
    if key in SAFETY_POSTURE_KEYS and not allow_safety_posture:
        return ("reject", "safety-posture")
    return ("write", canonical)


def import_toml_to_db(
    text: str,
    *,
    dry_run: bool = False,
    scan_terms: tuple[str, ...] | None = None,
    allow_safety_posture: bool = False,
) -> ConfigImport:
    """Load a ``config_setting export`` TOML dump into the ``ConfigSetting`` store — the export inverse.

    Retired aliases fold onto their live key; unknown keys and secret/personal-identifier
    rows are REJECTED (the whole import is refused if any row is rejected, so a bad key never
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
    """
    doc = tomllib.loads(text)
    terms = scan_terms if scan_terms is not None else resolve_export_scan_terms()
    stored: dict[str, dict[str, ConfigValue]] = {}
    by_kind: dict[str, list[ImportedRow]] = {"write": [], "skip": [], "unchanged": []}
    folded: list[tuple[str, str]] = []
    rejected: list[RejectedRow] = []
    for scope, raw_key, value in _import_candidates(doc):
        key = RENAMED_SETTING_KEYS.get(raw_key, raw_key)
        if key != raw_key:
            folded.append((raw_key, key))
        rows = stored.setdefault(scope, ConfigSetting.objects.overrides_for_scope(scope))
        kind, payload = _classify_import_row(
            key, value, terms, stored=rows.get(key), allow_safety_posture=allow_safety_posture
        )
        if kind == "reject":
            rejected.append(RejectedRow(scope, key, str(payload)))
        else:
            by_kind[kind].append(ImportedRow(scope, key, payload, is_safety_posture=key in SAFETY_POSTURE_KEYS))

    to_write, skipped, unchanged = by_kind["write"], by_kind["skip"], by_kind["unchanged"]
    seed_writes = _file_seed_dispositions(doc, skipped=skipped, unchanged=unchanged, rejected=rejected)
    if rejected:
        return ConfigImport((), tuple(skipped), tuple(folded), tuple(rejected), dry_run, tuple(unchanged))
    if not dry_run:
        for row in to_write:
            ConfigSetting.objects.set_value(row.key, row.value, scope=row.scope)
        for entry in seed_writes:
            write_seed_field(entry.table, entry.name, entry.field, entry.value)
    written = (*to_write, *(ImportedRow(e.scope, e.field, e.value) for e in seed_writes))
    return ConfigImport(written, tuple(skipped), tuple(folded), (), dry_run, tuple(unchanged))


def _file_seed_dispositions(
    doc: dict[str, Any],
    *,
    skipped: list[ImportedRow],
    unchanged: list[ImportedRow],
    rejected: list[RejectedRow],
) -> list[SeedFieldDisposition]:
    """File each seed field into *skipped* / *unchanged* / *rejected*; return the writes.

    An entry the shipped file carries can still have no DB row on a box that never ran the
    install seed, so a write onto a missing row is rejected rather than left to raise
    mid-import — the whole import is refused and the operator is told to seed first.
    """
    writes: list[SeedFieldDisposition] = []
    for entry in classify_seed_rows(doc):
        if entry.kind == "reject":
            rejected.append(RejectedRow(entry.scope, entry.field, entry.reason))
        elif entry.kind == "skip":
            skipped.append(ImportedRow(entry.scope, entry.field, entry.value))
        elif holds_value(entry):
            unchanged.append(ImportedRow(entry.scope, entry.field, entry.value))
        else:
            writes.append(entry)
    unseeded = unseeded_entries(writes)
    if not unseeded:
        return writes
    rejected.extend(
        RejectedRow(entry.scope, entry.field, f"no {entry.table} row yet — run `t3 setup` to seed it")
        for entry in writes
        if (entry.table, entry.name) in unseeded
    )
    return []
