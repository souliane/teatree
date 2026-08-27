"""The secret-withhold rule the config export and the config import BOTH apply.

One question — "must this row never be shared?" — asked on the way OUT of the store by
``export_db_to_toml`` and on the way back IN by ``import_toml_to_db``, so a shared TOML
can neither leak customer data nor smuggle it back. Stated once here because the two
directions must agree by construction: an export that withheld a class the import still
accepted would let a dump round-trip data the guard exists to keep out.

:func:`redaction_reason` asks that question of ONE key and value.
:func:`withhold` asks it of every key at every DEPTH of a value, because three of the four
withhold classes are key-name tests and a config value is frequently a TABLE of further
named values: an ``overlays`` registry row holds overlay definitions, and a preset's
``entries`` holds setting values. Neither container's own name says anything about the
credential coordinates inside it, so a rule that reads only the outer key is blind to them.

The rule has a FILE-level half too (#4156): ``--include-private`` emits exactly the rows an
ordinary import refuses, so such a file declares itself a personal backup and a restore is
an explicit act naming that marker. It lives here because it is the same question asked of
a whole document, and the two directions must agree on it for the same reason.

Withholding is what the guard does; deciding a row is not CONFIGURATION at all is a
different question, answered by :mod:`teatree.config.stored_row_health`.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import tomlkit

from teatree.config.secret_settings import PERSONAL_IDENTIFIERS, SECRET_SETTINGS, is_credential_reference
from teatree.core.models.config_setting import ConfigValue
from teatree.hooks.term_match import matched_term

#: The reserved top-level table a ``--include-private`` export stamps itself with. Its name
#: cannot collide: settings live under ``[teatree]``, overlays under ``[overlays.<name>]``,
#: e2e repos under ``[e2e_repos.<name>]``, and the seed families own their own three.
PRIVATE_BACKUP_TABLE = "backup"
_INCLUDE_PRIVATE_FIELD = "include_private"


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


def redaction_reason(key: str, value: ConfigValue | None, terms: tuple[str, ...]) -> str | None:
    """Why this row must not be shared, else None.

    ``value`` is optional: a key-only caller passes ``None`` to match just the
    value-independent classes, and the term scan json-dumps it either way.

    Four withhold classes, first match wins: an explicit private key
    (``SECRET_SETTINGS``); a credential coordinate (the SAME suffix rule the dashboard
    credential band uses — ``anthropic_oauth_pass_paths`` / ``*_credential_entry`` /
    ``*_token_ref`` etc.); a personal identifier (``slack_user_id`` /
    ``slack_user_channel``); or a value carrying a banned
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


@dataclass(frozen=True, slots=True)
class Withheld:
    """One value the guard withholds: why, and where inside its row it sat."""

    path: str  # "" when the WHOLE value was withheld, else the dotted path inside it
    reason: str


@dataclass(frozen=True, slots=True)
class Guarded:
    """A value with every withheld part *substitute*-d, and the report of what was withheld."""

    value: object
    withheld: tuple[Withheld, ...]


#: What to put where a withheld value was — the caller's choice, because the two surfaces
#: need opposite things: a snapshot substitutes a comparable stub, an export drops the field.
type Substitute = Callable[[str, object], object]


@dataclass(slots=True)
class _Walk:
    """One traversal's constants plus its accumulator — bundled so the recursion stays readable."""

    terms: tuple[str, ...]
    substitute: Substitute
    found: list[Withheld]

    def descend(self, value: object, path: tuple[str, ...]) -> object:
        if isinstance(value, dict):
            return {str(name): self.field(str(name), item, path) for name, item in value.items()}
        if isinstance(value, list):
            return [self.descend(item, (*path, str(index))) for index, item in enumerate(value)]
        return value

    def field(self, name: str, item: object, path: tuple[str, ...]) -> object:
        here = (*path, name)
        reason = redaction_reason(name, _as_config_value(item), self.terms)
        if reason is None:
            return self.descend(item, here)
        self.found.append(Withheld(".".join(here), reason))
        return self.substitute(reason, item)


def withhold(key: str, value: object, terms: tuple[str, ...], *, substitute: Substitute) -> Guarded:
    """*value* with every part :func:`redaction_reason` withholds replaced by *substitute*.

    The row key decides the whole row; every NESTED key is then asked the SAME question by the
    SAME classifier, so a credential coordinate is caught by the suffix rule that already names
    its class rather than by an entry per container that might carry one.
    """
    reason = redaction_reason(key, _as_config_value(value), terms)
    if reason is not None:
        return Guarded(substitute(reason, value), (Withheld("", reason),))
    walk = _Walk(terms=terms, substitute=substitute, found=[])
    return Guarded(walk.descend(value, ()), tuple(walk.found))


def withheld_within(key: str, value: object, terms: tuple[str, ...]) -> tuple[Withheld, ...]:
    """What :func:`withhold` would withhold from *value* — for a caller that only needs to know."""
    return withhold(key, value, terms, substitute=lambda _reason, held: held).withheld


def _as_config_value(value: object) -> ConfigValue:
    """The walk recurses over ``object``; the classifier only ever json-dumps what it is given."""
    if isinstance(value, bool | int | float | str):
        return value
    # isinstance cannot check a container's element types, so the two container arms are cast.
    return cast("ConfigValue", value) if isinstance(value, list | dict) else str(value)


def mark_private_backup(document: tomlkit.TOMLDocument) -> None:
    """Stamp *document* as a personal backup — the file-level half of the withhold rule.

    Called before anything else is attached so the declaration is the first thing a reader
    (and the import) meets. It is an intent marker, not a security boundary: the posture the
    guard rests on is the DEFAULT import refusing every private row whatever the file claims.
    """
    document.add(tomlkit.comment("PERSONAL BACKUP — carries the private rows a shared export withholds."))
    document.add(tomlkit.comment("Never share it; restore it with `config_setting import --restore-private`."))
    table = tomlkit.table()
    table[_INCLUDE_PRIVATE_FIELD] = True
    document[PRIVATE_BACKUP_TABLE] = table


def is_private_backup(doc: Mapping[str, Any]) -> bool:
    """Whether *doc* declares itself a ``--include-private`` personal backup."""
    table = doc.get(PRIVATE_BACKUP_TABLE)
    return isinstance(table, Mapping) and table.get(_INCLUDE_PRIVATE_FIELD) is True


__all__ = [
    "PRIVATE_BACKUP_TABLE",
    "Guarded",
    "RedactedRow",
    "Substitute",
    "Withheld",
    "is_private_backup",
    "mark_private_backup",
    "redaction_reason",
    "resolve_export_scan_terms",
    "withheld_within",
    "withhold",
]
