"""Reachability annotation for dependency-audit findings (#4346).

A raw CVE list forces the reader to re-derive the only question that decides
what to do: does the affected code run here? Django 6.0.8 fixed a High-severity
raster CVE that this codebase cannot reach and a Moderate admin one it can — the
severity column ranked those the wrong way round, and only the import graph said
so.

So each advisory is annotated against the imports actually present under
``src/``. The verdict is three-valued on purpose: a NEGATIVE is the reassuring
answer, so it is only reported when the distribution→import mapping is
authoritative (installed package metadata). A guessed mapping that finds nothing
reports ``UNKNOWN``, never ``NOT_IMPORTED``.

The index is a static lower bound — a settings string or an entry point can
reach a module no ``import`` statement names — so ``NOT_IMPORTED`` means "no
import path from ``src/``", not "provably dead".
"""

import ast
import json
import re
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import packages_distributions
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path


class Reach(Enum):
    IMPORTED = "imported"
    NOT_IMPORTED = "not_imported"
    UNKNOWN = "unknown"


class Basis(Enum):
    METADATA = "metadata"
    GUESSED = "guessed"


@dataclass(frozen=True)
class Advisory:
    package: str
    version: str
    vuln_id: str
    aliases: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class ComponentReach:
    module: str
    reach: Reach


@dataclass(frozen=True)
class AnnotatedAdvisory:
    advisory: Advisory
    import_names: tuple[str, ...]
    basis: Basis
    package_reach: Reach
    components: tuple[ComponentReach, ...]


class ReportError(ValueError):
    """The audit report could not be parsed — never degraded to an empty finding list."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"not a pip-audit JSON report: {detail}")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def build_import_index(src_root: "Path") -> frozenset[str]:
    """Every absolute module path imported by any ``*.py`` under *src_root*."""
    imported: set[str] = set()
    for path in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module)
    return frozenset(imported)


def resolve_import_names(
    distribution: str,
    *,
    distributions: "Mapping[str, list[str]] | None" = None,
) -> tuple[frozenset[str], Basis]:
    """Canonicalize a distribution name UP to the import name(s) that own it."""
    mapping = packages_distributions() if distributions is None else distributions
    wanted = _normalize(distribution)
    owned = {module for module, dists in mapping.items() if any(_normalize(d) == wanted for d in dists)}
    if owned:
        return frozenset(owned), Basis.METADATA
    return frozenset({re.sub(r"[-.]+", "_", distribution).lower()}), Basis.GUESSED


def _reaches(index: frozenset[str], module: str) -> bool:
    prefix = f"{module}."
    return any(entry == module or entry.startswith(prefix) for entry in index)


def _components_named(description: str, import_names: "Iterable[str]") -> tuple[str, ...]:
    found: set[str] = set()
    for name in import_names:
        pattern = rf"\b{re.escape(name)}(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
        found.update(re.findall(pattern, description))
    return tuple(sorted(found))


_DEPENDENCIES_NOT_A_LIST = "the report's 'dependencies' key is not a list"
_ENTRY_NOT_AN_OBJECT = "a 'dependencies' entry is not an object"


def parse_report(text: str) -> tuple[Advisory, ...]:
    """Advisories from a ``pip-audit --format json`` report."""
    try:
        parsed = json.loads(text)
        dependencies = parsed["dependencies"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReportError(str(exc)) from exc
    if not isinstance(dependencies, list):
        raise ReportError(_DEPENDENCIES_NOT_A_LIST)
    # pip-audit reports the same advisory once per source that carries it, so a
    # raw pass yields the same PYSEC id four times for one package.
    by_key: dict[tuple[str, str, str], Advisory] = {}
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ReportError(_ENTRY_NOT_AN_OBJECT)
        for vuln in dependency.get("vulns") or ():
            advisory = Advisory(
                package=str(dependency.get("name", "")),
                version=str(dependency.get("version", "")),
                vuln_id=str(vuln.get("id", "")),
                aliases=tuple(str(a) for a in vuln.get("aliases") or ()),
                description=str(vuln.get("description", "")),
            )
            key = (advisory.package, advisory.version, advisory.vuln_id)
            seen = by_key.get(key)
            if seen is None:
                by_key[key] = advisory
                continue
            merged_aliases = tuple(dict.fromkeys((*seen.aliases, *advisory.aliases)))
            # Duplicates carry differently-worded descriptions; the longest names
            # the affected component most often.
            longest = seen.description if len(seen.description) >= len(advisory.description) else advisory.description
            by_key[key] = Advisory(
                package=seen.package,
                version=seen.version,
                vuln_id=seen.vuln_id,
                aliases=merged_aliases,
                description=longest,
            )
    return tuple(by_key.values())


def annotate(
    advisories: "Iterable[Advisory]",
    *,
    index: frozenset[str],
    distributions: "Mapping[str, list[str]] | None" = None,
) -> tuple[AnnotatedAdvisory, ...]:
    mapping = packages_distributions() if distributions is None else distributions
    annotated: list[AnnotatedAdvisory] = []
    for advisory in advisories:
        import_names, basis = resolve_import_names(advisory.package, distributions=mapping)
        if any(_reaches(index, name) for name in import_names):
            package_reach = Reach.IMPORTED
        elif basis is Basis.METADATA:
            package_reach = Reach.NOT_IMPORTED
        else:
            package_reach = Reach.UNKNOWN
        components = tuple(
            ComponentReach(
                module=module,
                reach=Reach.IMPORTED if _reaches(index, module) else Reach.NOT_IMPORTED,
            )
            for module in _components_named(advisory.description, import_names)
        )
        annotated.append(
            AnnotatedAdvisory(
                advisory=advisory,
                import_names=tuple(sorted(import_names)),
                basis=basis,
                package_reach=package_reach,
                components=components,
            )
        )
    return tuple(annotated)


_PACKAGE_VERDICT = {
    Reach.IMPORTED: "REACHABLE from src/",
    Reach.NOT_IMPORTED: "NOT reachable from src/ (no import path)",
    Reach.UNKNOWN: (
        "UNKNOWN — the import name could not be resolved from installed metadata, "
        "so a negative would not be trustworthy"
    ),
}

_FOOTER = (
    "Reachability is a static lower bound over `import` statements under src/: a module named "
    "only by a settings string or an entry point is counted as unreachable. Treat NOT reachable "
    "as 'no import path found', not as 'proven dead'."
)


def format_report(annotated: "Iterable[AnnotatedAdvisory]") -> str:
    entries = tuple(annotated)
    if not entries:
        return "dependency-audit: no advisories to assess."
    lines = [f"dependency-audit: {len(entries)} advisory/advisories, assessed against src/", ""]
    for entry in entries:
        advisory = entry.advisory
        aliases = f" ({', '.join(advisory.aliases)})" if advisory.aliases else ""
        lines.append(f"{advisory.package} {advisory.version} — {advisory.vuln_id}{aliases}")
        names = ", ".join(entry.import_names)
        lines.append(f"  package: {_PACKAGE_VERDICT[entry.package_reach]} [import {names}, {entry.basis.value}]")
        if entry.components:
            lines.append("  components named in the advisory:")
            lines.extend(f"    {c.module} — {_PACKAGE_VERDICT[c.reach]}" for c in entry.components)
        else:
            lines.append("  components: none named in the advisory text; package-level verdict only")
        lines.append("")
    lines.append(_FOOTER)
    return "\n".join(lines)
