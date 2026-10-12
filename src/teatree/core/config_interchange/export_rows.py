"""Classify stored settings rows before TOML export."""

from dataclasses import dataclass

from teatree.config.stored_row_health import is_operator_configuration, stored_row_kind
from teatree.core.config_interchange.secret_guard import RedactedRow, redaction_reason
from teatree.core.config_interchange.types import OmittedRow
from teatree.core.models.config_setting import ConfigValue
from teatree.core.overlays.overlay_credentials import known_pass_key_credential


@dataclass
class _ExportGuard:
    """The secret-guard context threaded through every export emitter.

    ``include_private`` includes private rows for a personal backup; otherwise each row is
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
        if is_operator_configuration(key) or known_pass_key_credential(key) is not None:
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
