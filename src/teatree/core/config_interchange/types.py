"""What an export and an import RETURN — the report each direction hands its callers.

Held apart from ``migration`` (which produces them) so the CLI, the dashboard and the tests
can name a disposition without importing the interchange itself, and so the two directions'
vocabularies sit side by side: what an export withheld or omitted, and what an import wrote,
skipped, folded, left unchanged or refused.
"""

from dataclasses import dataclass

import tomlkit

from teatree.core.config_interchange.secret_guard import RedactedRow
from teatree.core.models.config_setting import ConfigValue


@dataclass(frozen=True)
class OmittedRow:
    """One stored row the export left out because it is not configuration at all."""

    scope: str
    key: str
    reason: str  # `stored_row_kind`: "internal state — …" / "retired — …" / "unknown — …"


@dataclass(frozen=True)
class ConfigExport:
    """A config-store export: the TOML text, the secret-withheld rows, the non-config rows."""

    toml: str
    redacted: tuple[RedactedRow, ...]
    omitted: tuple[OmittedRow, ...] = ()


@dataclass(frozen=True)
class RejectedRow:
    """One import row the validator refused, with the reason it was not stored."""

    scope: str
    key: str
    reason: str  # "unknown key" / "secret (<class>)" / "removed (<why>)" / "invalid: <msg>" / "safety-posture"


@dataclass(frozen=True)
class ImportedRow:
    """One import row that was (or, under ``dry_run``, would be) written to the store."""

    scope: str
    key: str
    value: ConfigValue
    is_safety_posture: bool = False

    @property
    def toml_value(self) -> str:
        """The value as the TOML literal the file carries it as — never Python ``repr``.

        A preview lists what a TOML file says, so it must say it in TOML. Rendered through
        ``str()`` the same value reads ``True`` where the file says ``true`` and
        ``['abc']`` where it says ``["abc"]``, which is a DIFFERENCE on screen between a
        value and itself (#4147).
        """
        return tomlkit.item(self.value).as_string()


@dataclass(frozen=True)
class ConfigImport:
    """The outcome of an ``import_toml_to_db`` run — all five dispositions, plus the mode.

    ``rejected`` non-empty means the import was REFUSED wholesale: nothing was written,
    even the clean rows, so a partial store can never result from one bad key.

    ``written`` is the CHANGES alone. A row the store already holds at that value is
    ``unchanged``: re-importing a box's own export is a no-op, and a preview that called
    those rows writes reported a store full of changes to an operator who had changed
    nothing (#4147).
    """

    written: tuple[ImportedRow, ...]
    skipped_default: tuple[ImportedRow, ...]
    folded: tuple[tuple[str, str], ...]  # (retired alias, canonical replacement)
    rejected: tuple[RejectedRow, ...]
    dry_run: bool
    unchanged: tuple[ImportedRow, ...] = ()

    @property
    def safety_posture_keys(self) -> tuple[str, ...]:
        """The safety-posture keys this run writes — what a preview must flag before an apply."""
        return tuple(row.key for row in self.written if row.is_safety_posture)


__all__ = ["ConfigExport", "ConfigImport", "ImportedRow", "OmittedRow", "RejectedRow"]
