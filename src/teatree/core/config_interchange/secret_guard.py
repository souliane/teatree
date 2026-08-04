"""The secret-withhold rule the config export and the config import BOTH apply.

One question — "must this row never be shared?" — asked on the way OUT of the store by
``export_db_to_toml`` and on the way back IN by ``import_toml_to_db``, so a shared TOML
can neither leak customer data nor smuggle it back. Stated once here because the two
directions must agree by construction: an export that withheld a class the import still
accepted would let a dump round-trip data the guard exists to keep out.

Withholding is what the guard does; deciding a row is not CONFIGURATION at all is a
different question, answered by :mod:`teatree.config.stored_row_health`.
"""

import json
from dataclasses import dataclass

from teatree.config.secret_settings import PERSONAL_IDENTIFIERS, SECRET_SETTINGS, is_credential_reference
from teatree.core.models.config_setting import ConfigValue
from teatree.hooks.term_match import matched_term


@dataclass(frozen=True)
class RedactedRow:
    """One export row withheld by the secret guard, with the reason it was dropped."""

    scope: str
    key: str
    reason: str  # "private-key" / "credential-coordinate" / "personal-identifier" / "banned-term:<term>"


def resolve_export_scan_terms() -> tuple[str, ...]:
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
    # Deferred (PLC0415): importing `teatree.hooks.banned_term_registry` at module scope
    # eagerly loads its heavy package __init__; keep this module's import light.
    from teatree.hooks.banned_term_registry import export_scan_terms  # noqa: PLC0415 — deferred: kept lazy

    return export_scan_terms()


def redaction_reason(key: str, value: ConfigValue, terms: tuple[str, ...]) -> str | None:
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


__all__ = ["RedactedRow", "redaction_reason", "resolve_export_scan_terms"]
