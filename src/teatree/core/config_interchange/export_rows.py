"""Classify stored settings rows before TOML export."""

from collections import Counter
from dataclasses import dataclass, field

from teatree.config.registries import TERM_REGISTRY
from teatree.config.stored_row_health import is_operator_configuration, stored_row_kind
from teatree.core.config_interchange.secret_guard import (
    BANNED_TERM,
    ExportPrivacy,
    RedactedRow,
    available_scan_terms,
    carries_banned_term,
    redaction_reason,
    resolve_export_scan_terms,
    withheld_within,
)
from teatree.core.config_interchange.types import OmittedRow
from teatree.core.models.config_setting import UNSCANNED_NAME_SCOPE, WITHHELD_NAME_SCOPE, ConfigValue
from teatree.core.overlays.overlay_credentials import known_pass_key_credential
from teatree.hooks.term_match import line_matches


@dataclass
class ExportGuard:
    """The secret-guard context threaded through every export emitter.

    A SHARED export withholds every class :func:`redaction_reason` names; a PRIVATE one
    carries the key classes and withholds only what carries a banned term; a
    ``PRIVATE_WITH_TERM_VALUES`` one withholds nothing, so its ``terms`` are empty while
    ``report_terms`` still vet the names its stderr report prints. Each withheld row is
    appended to the shared ``redacted`` accumulator.
    """

    privacy: ExportPrivacy
    terms: tuple[str, ...]
    report_terms: tuple[str, ...] | None
    redacted: list[RedactedRow] = field(default_factory=list)
    omitted: list[OmittedRow] = field(default_factory=list)

    @classmethod
    def for_export(cls, privacy: ExportPrivacy, scan_terms: tuple[str, ...] | None) -> "ExportGuard":
        """Refuses a scanning export with no term; the term-values backup needs none to proceed."""
        if privacy is ExportPrivacy.PRIVATE_WITH_TERM_VALUES:
            return cls(privacy, (), available_scan_terms() if scan_terms is None else scan_terms)
        terms = resolve_export_scan_terms() if scan_terms is None else scan_terms
        return cls(privacy, terms, terms)

    @property
    def private_backup(self) -> bool:
        return self.privacy is not ExportPrivacy.SHARED

    def reported_omissions(self) -> tuple[OmittedRow, ...]:
        """Each omitted row, named only where ``report_terms`` prove its names carry no term."""
        named: list[OmittedRow] = []
        unnamed: Counter[str] = Counter()
        for row in self.omitted:
            if self.report_terms is None or line_matches(f"{row.scope} {row.key}", self.report_terms):
                unnamed[row.reason] += 1
            else:
                named.append(row)
        scope = UNSCANNED_NAME_SCOPE if self.report_terms is None else WITHHELD_NAME_SCOPE
        return (*named, *(OmittedRow(scope, f"{count} row(s)", reason) for reason, count in unnamed.items()))

    def reason(self, key: str, value: ConfigValue) -> str | None:
        # The stored registry holds terms even when another source supplies the ones scanned.
        if key == TERM_REGISTRY and self.privacy is ExportPrivacy.PRIVATE:
            return BANNED_TERM
        if self.private_backup:
            return BANNED_TERM if carries_banned_term(key, value, self.terms) else None
        return redaction_reason(key, value, self.terms)

    def field_reason(self, key: str, value: ConfigValue) -> str | None:
        """:meth:`reason`, but asked at every depth of a SHARED value, as a seed field needs."""
        if self.private_backup:
            return self.reason(key, value)
        held = withheld_within(key, value, self.terms)
        return held[0].reason if held else None

    def withheld_name(self, name: str, rows: int) -> bool:
        """Withhold everything under *name* when it carries a term, reported without the name."""
        if not line_matches(name, self.terms):
            return False
        self.redacted.append(RedactedRow(WITHHELD_NAME_SCOPE, f"{rows} row(s)", BANNED_TERM))
        return True


def _configuration_rows(rows: dict[str, ConfigValue], scope: str, *, guard: ExportGuard) -> dict[str, ConfigValue]:
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
        if is_operator_configuration(key) or known_pass_key_credential(key) is not None:
            kept[key] = value
        else:
            guard.omitted.append(OmittedRow(scope, key, stored_row_kind(key)))
    return kept


def _exportable_rows(rows: dict[str, ConfigValue], scope: str, *, guard: ExportGuard) -> dict[str, ConfigValue]:
    """Drop the rows the guard withholds, recording each in ``guard.redacted``."""
    kept: dict[str, ConfigValue] = {}
    for key, value in rows.items():
        reason = guard.reason(key, value)
        if reason is None:
            kept[key] = value
        else:
            guard.redacted.append(RedactedRow(scope, key, reason))
    return kept
