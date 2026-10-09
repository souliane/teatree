"""What the interchange COVERS — the breadth an operator is told before they apply a file.

The export spans more than the settings store: the ``[teatree]`` / ``[overlays.*]`` /
``[e2e_repos.*]`` tables ``migration`` emits, plus the ``[loops.*]`` / ``[modes.*]`` /
``[schedules.*]`` families ``seed_tables`` emits. Pressing Export captures loop enablement,
preset entries and the weekly schedule; importing writes them back — a materially bigger
action than "restore my settings" reads as (souliane/teatree#4340).

The layout modules answer WHICH table a row goes in; this one answers what each table is,
in words, so a surface can state the reach at the point of use. Stated here, off the same
constants the writers use, rather than typed into a template — a table the copy did not
name is the drift the whole statement exists to prevent.
"""

from dataclasses import dataclass

from teatree.config.seed_defaults import SEED_TABLES
from teatree.core.config_interchange.document_layout import E2E_REPOS_TABLE, OVERLAYS_TABLE, TEATREE_TABLE


@dataclass(frozen=True, slots=True)
class ExportSection:
    """One top-level table of an export dump, and what it holds in the operator's words."""

    table: str
    label: str
    covers: str


#: Every top-level table an export can emit, in the order the dump writes them. The one
#: table outside it is ``secret_guard.PRIVATE_BACKUP_TABLE``, which declares a file's FORMAT
#: rather than carrying configuration.
EXPORT_SECTIONS: tuple[ExportSection, ...] = (
    ExportSection(
        TEATREE_TABLE,
        "Config settings",
        "every config key this box overrides at the global scope",
    ),
    ExportSection(
        OVERLAYS_TABLE,
        "Overlays",
        "each overlay's registry definition and its per-overlay setting scope",
    ),
    ExportSection(
        E2E_REPOS_TABLE,
        "E2E repos",
        "the registry of repositories the end-to-end lane runs against",
    ),
    ExportSection(
        "loops",
        "Loops",
        "each loop's enablement, cadence and description — importing changes which loops run",
    ),
    ExportSection(
        "modes",
        "Presets",
        "each preset's per-loop entries and description",
    ),
    ExportSection(
        "schedules",
        "Schedules",
        "each schedule's timezone and description — importing changes when presets take effect",
    ),
)


def section_for_row(scope: str, key: str) -> str:
    """The top-level table an import-report row came out of.

    A report row carries its own coordinates and each family spells them differently: a seed
    row's scope is ``<family>.<entry>``, a per-overlay setting's is the overlay name, and a
    global row's is empty — where only the two registry KEYS are tables of their own.
    """
    family, dot, _ = scope.partition(".")
    if dot and family in SEED_TABLES:
        return family
    if scope:
        return OVERLAYS_TABLE
    if key in {OVERLAYS_TABLE, E2E_REPOS_TABLE}:
        return key
    return TEATREE_TABLE


__all__ = ["EXPORT_SECTIONS", "ExportSection", "section_for_row"]
