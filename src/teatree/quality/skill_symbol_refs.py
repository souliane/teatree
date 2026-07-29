"""Resolve teatree-shaped symbol references in ``skills/**/*.md`` against the tree.

A worked example in a skill that names a plausible-but-absent teatree module,
path, or symbol reads to a skimming agent as a work item rather than an
illustration — agents have opened branches implementing the illustration. The
symbol has no Python import to break and no test to turn red, so nothing catches
it. This walk does: every teatree-shaped reference a skill makes must resolve
against the live tree.

Three reference shapes are extracted:

- a repo path under ``src/teatree/`` — must exist on disk.
- a dotted ``teatree.<...>`` name — the longest importable module prefix is
    imported and the remaining segments are walked with ``getattr``.
- a ``from teatree.<mod> import a, b`` statement — the module plus each
    imported name.

Exempt (never flagged):

- a line carrying ``skill-symbol-ref: <reason>``. When the next non-blank line
    opens a fenced code block the exemption covers that whole block. The reason
    is required, and documents why the reference resolves to nothing — a
    deliberately fictional illustration, an entry-point group, a config-section
    header.
- a dotted name inside a bracketed header (``[teatree.speak]``,
    ``[project.entry-points."teatree.overlays"]``) — a config section or
    entry-point group, not an importable name.
- a dotted name whose last segment is a file extension (``teatree.pth``).
- a path carrying a glob (``src/teatree/loop/scanners/*``).

The remedy for a fictional illustration is a placeholder namespace the walk does
not recognise as teatree-shaped (``src/acme/...``), not a pragma: a name outside
the tree cannot be misread as a work item in the first place.
"""

import importlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PRAGMA = re.compile(r"skill-symbol-ref:\s*\S")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_PATH = re.compile(r"src/teatree/[A-Za-z0-9_.][A-Za-z0-9_./*-]*")
_DOTTED = re.compile(r"\bteatree(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_FROM_IMPORT = re.compile(r"\bfrom\s+(teatree(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+import\s+(.+)")
_BRACKETED = re.compile(r"\[[A-Za-z0-9_.\"'-]+\](?!\()")
_NON_MODULE_TAILS = frozenset({"cfg", "json", "lock", "md", "pth", "py", "sqlite3", "toml", "txt", "yaml", "yml"})


@dataclass(frozen=True)
class SymbolRefFinding:
    """One teatree-shaped reference found in a skill file.

    ``reason`` is ``None`` when the reference resolves against the live tree,
    and a human-readable failure string when it does not.
    """

    path: Path
    lineno: int
    ref: str
    reason: str | None


def resolve_repo_path(ref: str, repo_root: Path) -> str | None:
    """Return ``None`` when ``ref`` names an existing path, else why not."""
    if (repo_root / ref).exists():
        return None
    return "no such path in the tree"


def resolve_dotted(dotted: str) -> str | None:
    """Resolve a dotted teatree name; return ``None`` if it resolves, else why not.

    The longest importable prefix wins, so a class or function tail
    (``teatree.core.notify.notify_user``) resolves through its module the same
    way an import would.
    """
    parts = dotted.split(".")
    for depth in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:depth]))
        except ImportError:
            continue
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            return f"{type(exc).__name__}: {exc}"
        return _walk_attributes(module, parts, depth)
    return f"no importable module in {dotted!r}"


def _walk_attributes(module: object, parts: list[str], depth: int) -> str | None:
    obj = module
    for index in range(depth, len(parts)):
        name = parts[index]
        if name in getattr(obj, "__annotations__", {}):
            return None
        if not hasattr(obj, name):
            return f"{'.'.join(parts[:index])} has no attribute {name!r}"
        obj = getattr(obj, name)
    return None


def _exempt_lines(source: str) -> set[int]:
    """Line numbers the pragma exempts — its own line, or the fenced block it introduces."""
    exempt: set[int] = set()
    pragma_seen = False
    inside_exempt_fence = False
    for lineno, line in enumerate(source.splitlines(), start=1):
        if _FENCE.match(line):
            if inside_exempt_fence or pragma_seen:
                exempt.add(lineno)
            inside_exempt_fence = pragma_seen and not inside_exempt_fence
            pragma_seen = False
            continue
        if inside_exempt_fence:
            exempt.add(lineno)
            continue
        if PRAGMA.search(line):
            pragma_seen = True
            exempt.add(lineno)
        elif line.strip():
            pragma_seen = False
    return exempt


def _bracketed_spans(line: str) -> list[tuple[int, int]]:
    return [match.span() for match in _BRACKETED.finditer(line)]


def _is_bracketed(span: tuple[int, int], spans: Iterable[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in spans)


def _imported_names(clause: str) -> list[str]:
    names = []
    for chunk in clause.strip().strip("()").split(","):
        token = chunk.strip().strip("()`\"'").split(" as ")[0].strip()
        if token.isidentifier():
            names.append(token)
    return names


def _refs_in_line(line: str) -> list[str]:
    refs: list[str] = []
    spans = _bracketed_spans(line)
    for match in _PATH.finditer(line):
        ref = match.group().rstrip("`.,;:)")
        if "*" not in ref:
            refs.append(ref)
    for match in _DOTTED.finditer(line):
        dotted = match.group()
        if _is_bracketed(match.span(), spans) or dotted.rsplit(".", 1)[-1] in _NON_MODULE_TAILS:
            continue
        refs.append(dotted)
    for match in _FROM_IMPORT.finditer(line):
        module, clause = match.group(1), match.group(2)
        refs.extend(f"{module}.{name}" for name in _imported_names(clause))
    return refs


def scan_source(source: str, path: Path, repo_root: Path) -> list[SymbolRefFinding]:
    """Extract and resolve every teatree-shaped reference in a skill document."""
    exempt = _exempt_lines(source)
    findings: list[SymbolRefFinding] = []
    seen: set[tuple[int, str]] = set()
    for lineno, line in enumerate(source.splitlines(), start=1):
        if lineno in exempt:
            continue
        for ref in _refs_in_line(line):
            if (lineno, ref) in seen:
                continue
            seen.add((lineno, ref))
            reason = resolve_repo_path(ref, repo_root) if ref.startswith("src/") else resolve_dotted(ref)
            findings.append(SymbolRefFinding(path=path, lineno=lineno, ref=ref, reason=reason))
    return findings


def scan_file(path: Path, repo_root: Path) -> list[SymbolRefFinding]:
    return scan_source(path.read_text(encoding="utf-8"), path, repo_root)


def scan_tree(skills_root: Path, repo_root: Path) -> list[SymbolRefFinding]:
    findings: list[SymbolRefFinding] = []
    for path in sorted(skills_root.rglob("*.md")):
        findings.extend(scan_file(path, repo_root))
    return findings
