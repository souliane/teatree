"""Classify each row of an import against the store — the inverse of :mod:`export_rows`."""

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from teatree.config import effective_default
from teatree.config.credential_pass_key import validate_pass_key_entry
from teatree.config.known_settings import ALL_KNOWN_CONFIG_SETTINGS, config_key_hint
from teatree.config.registries import REGISTRY_KEYS
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.config_interchange.document_layout import import_candidates
from teatree.core.config_interchange.registry_rows import merged_registry
from teatree.core.config_interchange.secret_guard import BANNED_TERM, redaction_reason, withheld_within
from teatree.core.config_interchange.seed_tables import (
    SeedFieldDisposition,
    classify_seed_rows,
    holds_value,
    mask_refusals,
    unseeded_entries,
)
from teatree.core.config_interchange.types import ImportedRow, RejectedRow
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import WITHHELD_NAME_SCOPE, ConfigValue
from teatree.core.overlays.overlay_credentials import known_pass_key_credential
from teatree.hooks.term_match import line_matches

_NAME_REFUSAL = f"secret ({BANNED_TERM})"
_REASON_QUOTES_A_TERM = "refused (reason withheld: quotes a banned term)"
_UNSCANNED_MASK_REFUSAL = "refused (reason withheld: not content-scanned)"


@dataclass(frozen=True)
class ImportPolicy:
    """What this import may do, threaded through the row classifier.

    Every authorization an import can carry, named in one place: the live ban terms the
    content scan reads (``None`` when the store holds none, so nothing is content-scanned),
    the operator's safety-posture declaration, and the two halves of the private-backup
    restore.
    """

    terms: tuple[str, ...] | None
    allow_safety_posture: bool
    private_backup: bool
    restore_private: bool

    @property
    def allow_private(self) -> bool:
        """Whether a private row may be stored — the CALLER asked AND the FILE declared it.

        Two independent halves, both required, which is what keeps ``restore_private`` from
        being a blanket relaxation of the row rule: it reaches only a file this tooling wrote
        as a personal backup (#4156).
        """
        return self.restore_private and self.private_backup

    @property
    def unscanned(self) -> bool:
        return self.terms is None

    def secret_reason(self, key: str, value: ConfigValue | None) -> str | None:
        """The withhold class of *key*/*value*; an unscanned import still applies the key classes."""
        return redaction_reason(key, value, self.terms or ())

    def shows_withheld(self, key: str, value: ConfigValue) -> bool:
        return self.unscanned or self.secret_reason(key, value) is not None

    def withholds_name(self, name: str) -> bool:
        return self.terms is not None and line_matches(name, self.terms)

    def seed_reason(self, field: str, value: ConfigValue) -> str | None:
        held = withheld_within(field, value, self.terms or ())
        return held[0].reason if held else None

    def seed_refusal(self, field: str, value: ConfigValue) -> str | None:
        return None if self.allow_private else self.seed_reason(field, value)


def _unstorable_reason(key: str, value: ConfigValue, policy: ImportPolicy) -> str | None:
    """Why this row has no home in the store at all, else None.

    An unknown key and a secret/personal-identifier row
    (reusing the export withhold rule so a shared TOML never smuggles customer data back in).
    ``policy.allow_private`` lifts only that last class, and only for a file that declared
    itself a personal backup — it is what makes ``--include-private`` restorable (#4156).
    """
    if key not in ALL_KNOWN_CONFIG_SETTINGS and known_pass_key_credential(key) is None:
        return f"unknown key; {config_key_hint(key)}"
    if policy.allow_private:
        return None
    if (secret := policy.secret_reason(key, value)) is not None:
        return f"secret ({secret})"
    return None


def _invalid_reason(key: str, value: ConfigValue, error: Exception, policy: ImportPolicy) -> str:
    """The parser message quotes the value, so it is dropped wherever the value is private."""
    if policy.unscanned:
        return "invalid (message withheld)"
    if (secret := policy.secret_reason(key, value)) is not None:
        return f"invalid ({secret} value; message withheld)"
    return f"invalid: {error}"


def classify_import_row(
    key: str, value: ConfigValue, *, stored: ConfigValue | None, policy: ImportPolicy
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
    if (unstorable := _unstorable_reason(key, value, policy)) is not None:
        return ("reject", unstorable)
    candidate = merged_registry(value, stored) if key in REGISTRY_KEYS and isinstance(value, dict) else value
    pass_key = known_pass_key_credential(key)
    try:
        canonical = (
            validate_pass_key_entry(candidate) if pass_key is not None else validate_config_write(key, candidate)
        )
    except (ConfigWriteError, ValueError) as exc:
        return ("reject", _invalid_reason(key, candidate, exc, policy))
    if pass_key is None and canonical == effective_default(key):
        return ("skip", canonical)
    if canonical == stored:
        return ("unchanged", canonical)
    if key in SAFETY_POSTURE_KEYS and not policy.allow_safety_posture:
        return ("reject", "safety-posture")
    return ("write", canonical)


@dataclass
class ImportRows:
    """Every row of one document, filed by disposition — the import's whole decision."""

    to_write: list[ImportedRow] = field(default_factory=list)
    skipped: list[ImportedRow] = field(default_factory=list)
    unchanged: list[ImportedRow] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    seed_writes: list[SeedFieldDisposition] = field(default_factory=list)


def classify_document(doc: dict[str, Any], policy: ImportPolicy) -> ImportRows:
    """File every settings row, then every seed field, of *doc*; nothing is written here."""
    rows = ImportRows()
    by_kind = {"write": rows.to_write, "skip": rows.skipped, "unchanged": rows.unchanged}
    stored: dict[str, dict[str, ConfigValue]] = {}
    withheld_names: Counter[str] = Counter()
    for scope, key, value in import_candidates(doc):
        name_withheld = policy.withholds_name(scope)
        if name_withheld and not policy.allow_private:
            withheld_names[scope] += 1
            continue
        held = stored.setdefault(scope, ConfigSetting.objects.overrides_for_scope(scope))
        kind, payload = classify_import_row(key, value, stored=held.get(key), policy=policy)
        if kind == "reject":
            rows.rejected.append(RejectedRow(WITHHELD_NAME_SCOPE if name_withheld else scope, key, str(payload)))
            continue
        by_kind[kind].append(
            ImportedRow(
                scope,
                key,
                payload,
                is_safety_posture=key in SAFETY_POSTURE_KEYS,
                is_private=policy.shows_withheld(key, payload),
                name_withheld=name_withheld,
            )
        )
    rows.rejected.extend(name_refusals(withheld_names))
    rows.seed_writes = file_seed_dispositions(
        doc, policy, skipped=rows.skipped, unchanged=rows.unchanged, rejected=rows.rejected
    )
    return rows


def name_refusals(withheld: Counter[str]) -> list[RejectedRow]:
    """One name-free rejection per withheld scope or entry name, counting the rows under it."""
    return [RejectedRow(WITHHELD_NAME_SCOPE, f"{count} row(s)", _NAME_REFUSAL) for count in withheld.values()]


def seed_row(entry: SeedFieldDisposition, policy: ImportPolicy) -> ImportedRow:
    is_private = policy.unscanned or policy.seed_reason(entry.field, entry.value) is not None
    name_withheld = policy.withholds_name(entry.name)
    return ImportedRow(entry.scope, entry.field, entry.value, is_private=is_private, name_withheld=name_withheld)


def _seed_rejection(entry: SeedFieldDisposition, reason: str, policy: ImportPolicy) -> RejectedRow:
    scope = WITHHELD_NAME_SCOPE if policy.withholds_name(entry.name) else entry.scope
    shown = _REASON_QUOTES_A_TERM if line_matches(reason, policy.terms or ()) else reason
    return RejectedRow(scope, entry.field, shown)


def file_seed_dispositions(
    doc: dict[str, Any],
    policy: ImportPolicy,
    *,
    skipped: list[ImportedRow],
    unchanged: list[ImportedRow],
    rejected: list[RejectedRow],
) -> list[SeedFieldDisposition]:
    """File each seed field into *skipped* / *unchanged* / *rejected*; return the writes.

    A field the guard withholds is refused unless a personal backup is being restored, and
    an entry whose NAME carries a term is refused without the name on the same terms — both
    before the mask and seeding checks, so neither can echo what was refused. A restored one
    is reported under the withheld-name label, its refusals included, and a refusal reason
    quoting a term is withheld wherever it appears.

    An entry the shipped file carries can still have no DB row on a box that never ran the
    install seed, so a write onto a missing row is rejected rather than left to raise
    mid-import — the whole import is refused and the operator is told to seed first. A mode
    mask ``Mode.clean()`` would refuse is rejected the same way.
    """
    writes: list[SeedFieldDisposition] = []
    withheld_names: Counter[str] = Counter()
    for entry in classify_seed_rows(doc):
        if policy.withholds_name(entry.name) and not policy.allow_private:
            withheld_names[entry.scope] += 1
        elif entry.kind == "reject":
            rejected.append(_seed_rejection(entry, entry.reason, policy))
        elif (secret := policy.seed_refusal(entry.field, entry.value)) is not None:
            rejected.append(RejectedRow(entry.scope, entry.field, f"secret ({secret})"))
        elif entry.kind == "skip":
            skipped.append(seed_row(entry, policy))
        elif holds_value(entry):
            unchanged.append(seed_row(entry, policy))
        else:
            writes.append(entry)
    rejected.extend(name_refusals(withheld_names))
    unseeded = unseeded_entries(writes)
    # A mask refusal quotes the file's mask keys and the box's loop names.
    rejected.extend(
        _seed_rejection(entry, _UNSCANNED_MASK_REFUSAL if policy.unscanned else reason, policy)
        for entry, reason in mask_refusals(writes)
        if (entry.table, entry.name) not in unseeded
    )
    if not unseeded:
        return writes
    rejected.extend(
        _seed_rejection(entry, f"no {entry.table} row yet — run `t3 setup` to seed it", policy)
        for entry in writes
        if (entry.table, entry.name) in unseeded
    )
    return []
