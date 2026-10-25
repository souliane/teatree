"""The 17.4.4 merge-route table names merge entry points that exist, and only those."""

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import teatree.core.merge as merge_package

_BLUEPRINT = Path(__file__).resolve().parents[3] / "docs" / "blueprint" / "factory-architecture.md"
_SECTION_START = "#### 17.4.4 Post-merge audit"
_SECTION_END = "### 17.5 "
_WRITER_COLUMN = 2
_NAMED_ENTRIES = {"merge_ticket_pr", "merge_without_clear", "record_merge_and_advance"}


def _entries_named_by_the_route_table() -> set[str]:
    section = _BLUEPRINT.read_text(encoding="utf-8").split(_SECTION_START, 1)[1].split(_SECTION_END, 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")][2:]
    return {name for row in rows for name in re.findall(r"`([a-z_]+)`", row.split("|")[_WRITER_COLUMN])}


def _functions_defined_under_core_merge() -> set[str]:
    names: set[str] = set()
    for info in pkgutil.iter_modules(merge_package.__path__, f"{merge_package.__name__}."):
        module = importlib.import_module(info.name)
        names |= {
            name
            for name, member in vars(module).items()
            if inspect.isfunction(member) and member.__module__ == module.__name__
        }
    return names


def test_the_route_table_names_the_expected_entries() -> None:
    assert _entries_named_by_the_route_table() == _NAMED_ENTRIES


def test_every_named_entry_resolves_to_a_function_under_core_merge() -> None:
    assert _functions_defined_under_core_merge() >= _NAMED_ENTRIES
