"""The import/export page's view models — the export seam, and the breadth of an import.

The transfer control is a page of its own rather than a band on the settings page, because
the file it reads and writes reaches past the settings store into the loop, preset and
schedule rows (souliane/teatree#4340). Both halves of stating that live here: the page shows
:data:`~teatree.core.config_interchange.scope.EXPORT_SECTIONS` up front, and a preview is
counted PER section so an operator sees it is about to change loop or schedule state before
they apply rather than after.

The two seam wrappers are thin on purpose: the interchange itself is core's, and this module
only fixes the page's policy on it (secrets withheld, a dry run before any write).
"""

from collections import Counter
from dataclasses import dataclass

from teatree.core.config_interchange.migration import export_db_to_toml, import_toml_to_db
from teatree.core.config_interchange.scope import EXPORT_SECTIONS, ExportSection, section_for_row
from teatree.core.config_interchange.types import ConfigImport


@dataclass(frozen=True, slots=True)
class SectionChange:
    """One section of the dump, and how many of its rows an import would change."""

    section: ExportSection
    count: int


def export_text(*, default_keys_only: bool = False, include_defaults: bool = False) -> str:
    """The shareable export dump — secrets withheld, personal kept (Phase-4 semantics).

    The two filters are the page's two checkboxes, both unticked by default so the plain
    download is the delta dump it has always been. Ticking both yields the ``defaults.toml``
    shape: a complete, drop-in replacement for the shipped file.
    """
    return export_db_to_toml(
        include_private=False,
        default_keys_only=default_keys_only,
        include_defaults=include_defaults,
    ).toml


def import_preview(text: str) -> ConfigImport:
    """Classify an import WITHOUT writing — the dry-run preview of what would change.

    Classifies as if the safety-posture keys were authorized so the preview can SHOW and flag
    them; nothing is written, and the apply path re-runs the classification with the operator's
    actual authorization.
    """
    return import_toml_to_db(text, dry_run=True, allow_safety_posture=True)


def changed_sections(result: ConfigImport) -> tuple[SectionChange, ...]:
    """*result*'s written rows counted per section, in the order the dump writes them.

    Only the sections a file actually touches are returned, so the line reads as what THIS
    file does rather than as a checklist of everything the format can carry.
    """
    counts = Counter(section_for_row(row.scope, row.key) for row in result.written)
    return tuple(SectionChange(section, counts[section.table]) for section in EXPORT_SECTIONS if counts[section.table])


__all__ = ["SectionChange", "changed_sections", "export_text", "import_preview"]
