"""Resolve teatree-shaped symbol references in ``skills/**/*.md`` against the tree.

A worked example in a skill that names a plausible-but-absent teatree module,
path, or symbol reads to a skimming agent as a work item rather than an
illustration — agents have opened branches implementing the illustration. The
symbol has no Python import to break and no test to turn red, so nothing catches
it. This walk does: every teatree-shaped reference a skill makes must resolve
against the live tree.

Six reference shapes are extracted:

- a repo path under ``src/teatree/`` — must exist on disk.
- a repo path under any other top-level directory of the tree — checked only
    when it names a file under a directory that exists, because outside
    ``src/teatree/`` those top-level names recur in prose (``dev/staging``) and
    in stand-in namespaces (``src/acme/billing/sweep.py``).
- a dotted ``teatree.<...>`` name — the longest importable module prefix is
    imported and the remaining segments are walked with ``getattr``.
- a bare module-local ``<module>.<SYMBOL>`` name (``phase_tools.PHASE_TOOLS``,
    ``hook_router._teatree_engaged``) — resolved against every indexed module of
    that basename. Anchoring only on the literal ``teatree`` missed this shape
    entirely, and a resolving path citation beside it made it read as vouched
    for. Widening is bounded at both ends so third-party and attribute-access
    tokens stay out: the head must name a module the tree actually ships, and
    the tail must be module-symbol-shaped (``UPPER_SNAKE`` or ``_private``), so
    ``typer.Exit``, ``permissions.allow`` and ``overlay.config`` are not ours.
- a path-qualified ``<dir>/<module>.<SYMBOL>`` name
    (``hooks/scripts/main_clone_guard.handle_block_main_clone_mutation``) — the
    module-local reading with the directory kept as the qualifier. The
    reference admits two readings and only the path one was tried, so a live
    symbol read as an absent path; the path reading still goes first, so a
    genuine path miss is caught, and the qualifier discriminates rather than
    being stripped, so a same-basename module in another directory cannot vouch
    for the reference.
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
- a path carrying an elision — a glob (``src/teatree/loop/scanners/*``) or an
    ellipsis (``src/...py``).

The remedy for a fictional illustration is a placeholder namespace the walk does
not recognise as teatree-shaped (``src/acme/...``), not a pragma: a name outside
the tree cannot be misread as a work item in the first place.
"""

import importlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PRAGMA = re.compile(r"skill-symbol-ref:\s*\S")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_PATH = re.compile(r"src/teatree/[A-Za-z0-9_.][A-Za-z0-9_./*-]*")
_DOTTED = re.compile(r"\bteatree(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_FROM_IMPORT = re.compile(r"\bfrom\s+(teatree(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s+import\s+(.+)")
_BRACKETED = re.compile(r"\[[A-Za-z0-9_.\"'-]+\](?!\()")
_BARE = re.compile(r"(?<![\w./-])[a-z_][a-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_MODULE_SYMBOL = re.compile(r"[A-Z][A-Z0-9_]*|_[A-Za-z0-9_]+")
_NON_MODULE_TAILS = frozenset({"cfg", "json", "lock", "md", "pth", "py", "sqlite3", "toml", "txt", "yaml", "yml"})
_ELISIONS = ("*", "...")
_TEATREE_SRC = "src/teatree/"
_INDEXED_PACKAGES = ("src/teatree", "hooks")
#: A slash-bearing reference whose tail names a file, so only the path reading applies.
_NO_SYMBOL_READING = "the tail names no module symbol"


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


@dataclass(frozen=True)
class RepoIndex:
    """What the live tree offers a skill's bare references to resolve against."""

    repo_root: Path
    modules: dict[str, tuple[str, ...]]
    path_pattern: re.Pattern[str]

    def is_citation(self, ref: str) -> bool:
        """Whether a path reference names a repo file rather than prose or a stand-in.

        Outside ``src/teatree/`` the tree's own top-level names recur in prose
        (``dev/staging``, ``docs/skills``) and in stand-in namespaces
        (``src/acme/billing/sweep.py``), so a citation has to look like a file
        the tree could hold — a suffix, under a directory that exists.
        """
        if ref.startswith(_TEATREE_SRC):
            return True
        candidate = self.repo_root / ref
        return bool(candidate.suffix) and candidate.parent.is_dir()


@lru_cache(maxsize=8)
def build_repo_index(repo_root: Path) -> RepoIndex:
    """Index the importable modules and top-level directories a skill can name."""
    modules: dict[str, list[str]] = {}
    for package in _INDEXED_PACKAGES:
        for source in sorted((repo_root / package).rglob("*.py")):
            parts = source.relative_to(repo_root).with_suffix("").parts
            parts = parts[1:] if parts[0] == "src" else parts
            parts = parts[:-1] if parts[-1] == "__init__" else parts
            modules.setdefault(parts[-1], []).append(".".join(parts))
    tops = sorted(re.escape(entry.name) for entry in repo_root.iterdir() if entry.is_dir() and entry.name[0] != ".")
    return RepoIndex(
        repo_root=repo_root,
        modules={basename: tuple(dotted) for basename, dotted in modules.items()},
        path_pattern=re.compile(rf"(?<![\w/.-])(?:{'|'.join(tops)})/[A-Za-z0-9_.][A-Za-z0-9_./*-]*"),
    )


def resolve_repo_path(ref: str, repo_root: Path) -> str | None:
    """Return ``None`` when ``ref`` names an existing path, else why not."""
    if (repo_root / ref).exists():
        return None
    return "no such path in the tree"


def resolve_module_local(ref: str, index: RepoIndex) -> str | None:
    """Resolve ``<module>.<SYMBOL>`` against every indexed module of that basename.

    A basename the tree ships more than once resolves when any one of them
    carries the symbol — the reference is unqualified, so any reading that holds
    is a reading the skill's reader can take.
    """
    head, _, tail = ref.partition(".")
    reasons: list[str] = []
    for dotted in index.modules.get(head, ()):
        reason = resolve_dotted(f"{dotted}.{tail}")
        if reason is None:
            return None
        reasons.append(reason)
    return "; ".join(dict.fromkeys(reasons)) or f"no module named {head!r} in the tree"


def resolve_path_qualified_symbol(ref: str, index: RepoIndex) -> str | None:
    """Resolve ``<dir>/<module>.<SYMBOL>`` as the module symbol its tail names.

    The directory is carried into the candidate's dotted name rather than
    discarded, so only a module living under it can answer:
    ``hooks/scripts/main_clone_guard.handle_block_main_clone_mutation`` resolves
    against the hook leaf alone, never against the same-basename
    ``core/gates/`` module beside it.
    """
    directory, _, tail = ref.rpartition("/")
    head, _, symbol = tail.partition(".")
    if not symbol or symbol.partition(".")[0] in _NON_MODULE_TAILS:
        return _NO_SYMBOL_READING
    qualified = f"{_dotted_prefix(directory)}.{head}"
    reasons: list[str] = []
    for dotted in index.modules.get(head, ()):
        if dotted != qualified and not dotted.endswith(f".{qualified}"):
            continue
        reason = resolve_dotted(f"{dotted}.{symbol}")
        if reason is None:
            return None
        reasons.append(reason)
    return "; ".join(dict.fromkeys(reasons)) or f"no module named {qualified!r} in the tree"


def _dotted_prefix(directory: str) -> str:
    """A repo directory as the dotted prefix the module index keys it under."""
    parts = directory.split("/")
    return ".".join(parts[1:] if parts[0] == "src" else parts)


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


def _path_refs(line: str, index: RepoIndex) -> Iterator[str]:
    for pattern in (_PATH, index.path_pattern):
        for match in pattern.finditer(line):
            ref = match.group().rstrip("`.,;:)")
            if not any(elision in ref for elision in _ELISIONS) and index.is_citation(ref):
                yield ref


def _refs_in_line(line: str, index: RepoIndex) -> list[str]:
    refs: list[str] = list(_path_refs(line, index))
    spans = _bracketed_spans(line)
    for match in _DOTTED.finditer(line):
        dotted = match.group()
        if _is_bracketed(match.span(), spans) or dotted.rsplit(".", 1)[-1] in _NON_MODULE_TAILS:
            continue
        refs.append(dotted)
    for match in _BARE.finditer(line):
        bare = match.group()
        head, _, tail = bare.partition(".")
        if _is_bracketed(match.span(), spans) or head not in index.modules:
            continue
        if _MODULE_SYMBOL.fullmatch(tail.partition(".")[0]):
            refs.append(bare)
    for match in _FROM_IMPORT.finditer(line):
        module, clause = match.group(1), match.group(2)
        refs.extend(f"{module}.{name}" for name in _imported_names(clause))
    return refs


def _resolve(ref: str, index: RepoIndex) -> str | None:
    """Try every reading the reference admits; unresolved only when all of them fail."""
    if "/" not in ref:
        return resolve_dotted(ref) if ref.partition(".")[0] == "teatree" else resolve_module_local(ref, index)
    path_reason = resolve_repo_path(ref, index.repo_root)
    if path_reason is None:
        return None
    symbol_reason = resolve_path_qualified_symbol(ref, index)
    if symbol_reason is None:
        return None
    return path_reason if symbol_reason == _NO_SYMBOL_READING else f"{path_reason}; {symbol_reason}"


def scan_source(source: str, path: Path, repo_root: Path) -> list[SymbolRefFinding]:
    """Extract and resolve every teatree-shaped reference in a skill document."""
    index = build_repo_index(repo_root)
    exempt = _exempt_lines(source)
    findings: list[SymbolRefFinding] = []
    seen: set[tuple[int, str]] = set()
    for lineno, line in enumerate(source.splitlines(), start=1):
        if lineno in exempt:
            continue
        for ref in _refs_in_line(line, index):
            if (lineno, ref) in seen:
                continue
            seen.add((lineno, ref))
            findings.append(SymbolRefFinding(path=path, lineno=lineno, ref=ref, reason=_resolve(ref, index)))
    return findings


def scan_file(path: Path, repo_root: Path) -> list[SymbolRefFinding]:
    return scan_source(path.read_text(encoding="utf-8"), path, repo_root)


def scan_tree(skills_root: Path, repo_root: Path) -> list[SymbolRefFinding]:
    findings: list[SymbolRefFinding] = []
    for path in sorted(skills_root.rglob("*.md")):
        findings.extend(scan_file(path, repo_root))
    return findings
