"""Safety-biased incremental test selection (#113).

Fast-feedback ONLY. The whole-tree sharded run stays the merge/coverage gate; this
selector is opt-in local tooling and is NEVER wired into the pre-push gate.

Given a diff, decide which pytest test files to run. A changed ``src/teatree/**``
module expands to its transitive dependents — the reverse-import closure from
``tach map --direction dependents`` — and every test whose first-party imports hit
any module in that closure is selected, unioned with the mirror-convention test path
and an always-run floor. ANY change the classifier cannot prove local (conftest,
settings, migrations, non-``.py`` data files, deletions/renames, files outside the
modelled roots) degrades to a whole-tree FULL run. Under-run is a false green — the
same doctrine as :mod:`teatree.quality.changed_set`, the shared changed-set +
FULL-trigger normalizer this builds on.

Over-run is NOT free, though (#3645). One implementer's one-module fix escalated to
30182 tests over 59m32s because the diff carried the ``BLUEPRINT.md`` edit the
blueprint-sync gate compels — the doc edit was out-of-root, so it forced FULL. Docs
are therefore classified rather than blanket-escalated: :mod:`teatree.quality.doc_impact`
proves a path carries no executable semantics and maps it to the tests that READ it.
The escalation stays exactly as conservative for everything executable.
"""

import ast
import json
import shutil
import sys
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.changed_set import ChangedSet, ChangedSetError, FullTrigger, changed_paths, classify, is_migration
from teatree.quality.doc_impact import disk_doc_reader_lookup, is_doc_path, reference_tokens
from teatree.quality.test_path_mirror import collect_test_files, expected_test_dir
from teatree.utils.run import run_allowed_to_fail

#: Cross-cutting, subprocess-heavy suites an import graph cannot fully model — run
#: them on EVERY scoped selection so their blind spot is a constant cost, not a skip.
FLOOR_DIRS: tuple[str, ...] = ("tests/quality", "tests/integration", "tests/conformance")

_FIRST_PARTY_ROOT = "teatree"
_SRC_MODULE_PREFIX = "src/teatree/"
_SRC_PREFIX = "src/"
_TESTS_PREFIX = "tests/"
_TESTS_CONFIG_PREFIX = "tests/config/"


class TachUnavailableError(RuntimeError):
    """Raised when the tach dependency map cannot be produced — the caller runs FULL."""


@dataclass(frozen=True)
class SelectionVerdict:
    full: bool
    reason: str
    create_db: bool
    scoped_src: tuple[Path, ...] = ()
    scoped_tests: tuple[Path, ...] = ()
    scoped_docs: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectionReason:
    test: str
    kind: str  # self-changed | import-match | mirror
    chain: tuple[str, ...]


@dataclass(frozen=True)
class Selection:
    full: bool
    reason: str
    create_db: bool = False
    test_files: tuple[str, ...] = ()
    floor_dirs: tuple[str, ...] = FLOOR_DIRS
    doctest_targets: tuple[str, ...] = ()
    reasons: tuple[SelectionReason, ...] = ()
    changed_src: tuple[str, ...] = ()
    changed_tests: tuple[str, ...] = ()
    changed_docs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def pytest_args(self, *, test_db_cloned: bool = False) -> list[str]:
        """Positional pytest args for this selection.

        ``create_db`` normally emits ``--create-db`` (pytest-django replays every
        migration from zero). When the caller has already refreshed the test DB via
        the opt-in template clone (:func:`teatree.utils.django_db.prepare_test_db`,
        souliane/teatree#3326), pass ``test_db_cloned=True`` so the same drift
        instead emits ``--reuse-db``: the freshly cloned DB is current, and a
        ``--create-db`` would wipe it and replay. Default (``False``) is
        byte-identical to the pre-#3326 behaviour.
        """
        db = (["--reuse-db"] if test_db_cloned else ["--create-db"]) if self.create_db else []
        if self.full:
            return db  # empty positionals ⇒ the runner executes the whole suite
        doctest = ["--doctest-modules", *self.doctest_targets] if self.doctest_targets else []
        return [*db, *self.test_files, *doctest, *self.floor_dirs]

    def report(self) -> str:
        if self.full:
            return f"affected-tests: FULL — {self.reason}"
        return (
            f"affected-tests: SCOPED — {len(self.test_files)} test file(s) + "
            f"{len(self.floor_dirs)} floor dir(s), {len(self.doctest_targets)} changed src module(s), "
            f"{len(self.changed_docs)} changed doc(s); full-run triggers: none"
        )

    def explain(self, test: str | None = None) -> list[str]:
        chosen = [reason for reason in self.reasons if test is None or reason.test == test]
        if test is not None and not chosen:
            return [f"{test}: not selected by this diff"]
        return [f"{reason.test} [{reason.kind}]: " + " -> ".join(reason.chain) for reason in chosen]


def module_of(path: str) -> str | None:
    """Map ``src/teatree/foo/bar.py`` → ``teatree.foo.bar`` (``__init__`` → the package)."""
    if not (path.startswith(_SRC_MODULE_PREFIX) and path.endswith(".py")):
        return None
    dotted = path[len(_SRC_PREFIX) : -len(".py")].replace("/", ".")
    return dotted.removesuffix(".__init__")


def _extra_full_trigger(path: str) -> str | None:
    """#113-only FULL triggers beyond the shared changed-set classifier.

    ``changed_set.classify`` scopes a changed ``tests/**/*.py`` to itself (correct for
    the push gate's doctest/ast-grep sweep). Three of those change test SEMANTICS
    suite-wide, so for test SELECTION they must instead force FULL.
    """
    name = Path(path).name
    if name == "factories.py":
        return "factories.py changes shared fixtures suite-wide"
    if path.startswith(_TESTS_CONFIG_PREFIX):
        return "tests/config settings affect the whole suite"
    if path.startswith(_TESTS_PREFIX) and name.startswith("django_settings"):
        return "django settings affect the whole suite"
    return None


def classify_selection(changed: ChangedSet) -> SelectionVerdict:
    """Route a diff to FULL or the scoped src+test+doc lists, reusing the shared classifier.

    Docs are partitioned out FIRST (#3645) so a markdown/docs-tree/mkdocs path never
    reaches the shared classifier's out-of-root escalation — its impact is mapped to
    the tests that read it instead. Everything else keeps the #113 escalations on top
    of :func:`teatree.quality.changed_set.classify`: factories/settings force FULL, a
    migration additionally requires ``--create-db``, and any remaining path the shared
    classifier merely IGNORED still becomes FULL.
    """
    docs = tuple(sorted({entry.path for entry in changed.entries if is_doc_path(entry.path)}))
    executable = ChangedSet(
        entries=tuple(entry for entry in changed.entries if not is_doc_path(entry.path)),
        base_ref=changed.base_ref,
    )
    base: FullTrigger = classify(executable)
    create_db = any(is_migration(path) for path in executable.paths)
    if base.full:
        return SelectionVerdict(full=True, reason=base.reason, create_db=create_db)

    for entry in executable.entries:
        extra = _extra_full_trigger(entry.path)
        if extra:
            return SelectionVerdict(full=True, reason=f"{extra} ({entry.path})", create_db=create_db)

    scoped = {str(p) for p in base.scoped_src} | {str(p) for p in base.scoped_tests}
    ignored = [path for path in executable.paths if path not in scoped]
    if ignored:
        return SelectionVerdict(
            full=True,
            reason=f"non-code/out-of-root file a data-driven or doc test may read ({ignored[0]}) — FULL (fail-safe)",
            create_db=create_db,
        )
    return SelectionVerdict(
        full=False,
        reason=base.reason,
        create_db=create_db,
        scoped_src=base.scoped_src,
        scoped_tests=base.scoped_tests,
        scoped_docs=docs,
    )


def dependents_closure(seeds: Iterable[str], dependents_map: Mapping[str, list[str]]) -> dict[str, str | None]:
    """Transitive reverse-import closure of *seeds* with parent pointers (for --explain).

    Each closure file maps to the file that pulled it in (a seed maps to ``None``).
    ``dependents_map[f]`` are the files that DEPEND ON ``f`` (tach's
    ``--direction dependents`` adjacency), so BFS from a changed seed reaches every
    transitive dependent.
    """
    parent: dict[str, str | None] = {}
    queue: deque[str] = deque()
    for seed in seeds:
        if seed not in parent:
            parent[seed] = None
            queue.append(seed)
    while queue:
        node = queue.popleft()
        for dependent in dependents_map.get(node, ()):
            if dependent not in parent:
                parent[dependent] = node
                queue.append(dependent)
    return parent


def _prefixes(module: str) -> list[str]:
    parts = module.split(".")
    return [".".join(parts[: index + 1]) for index in range(len(parts))]


@dataclass(frozen=True)
class _ClosureIndex:
    """The closure files indexed by exact module and by every ancestor prefix."""

    module_to_file: dict[str, str]
    prefix_to_file: dict[str, str]


def _closure_index(parent: Mapping[str, str | None]) -> _ClosureIndex:
    module_to_file: dict[str, str] = {}
    prefix_to_file: dict[str, str] = {}
    for file in sorted(parent):
        module = module_of(file)
        if module is None:
            continue
        module_to_file.setdefault(module, file)
        for prefix in _prefixes(module):
            prefix_to_file.setdefault(prefix, file)
    return _ClosureIndex(module_to_file=module_to_file, prefix_to_file=prefix_to_file)


def _match_closure_file(
    imported: str, module_to_file: Mapping[str, str], prefix_to_file: Mapping[str, str]
) -> str | None:
    """The closure file a test's import overlaps, or ``None`` (over-selects on hierarchy).

    A match holds when the import is an ancestor-or-equal of a closure module
    (``imported in prefix_to_file``) OR a closure module is an ancestor-or-equal of the
    import (an ancestor of ``imported`` is an exact closure module) — the symmetric
    prefix overlap covers the ``from teatree.foo import bar`` granularity gap.
    """
    if imported in prefix_to_file:
        return prefix_to_file[imported]
    for ancestor in reversed(_prefixes(imported)):
        if ancestor in module_to_file:
            return module_to_file[ancestor]
    return None


def _seed_chain(node: str, parent: Mapping[str, str | None]) -> list[str]:
    trail = [node]
    current = parent.get(node)
    while current is not None:
        trail.append(current)
        current = parent.get(current)
    trail.reverse()
    return trail


def _import_chain(test: str, imported: str, closure_file: str, parent: Mapping[str, str | None]) -> tuple[str, ...]:
    trail = _seed_chain(closure_file, parent)
    annotated = [file + (" (changed)" if index == 0 else " (dependent)") for index, file in enumerate(trail)]
    annotated.append(f"{test} imports {imported}")
    return tuple(annotated)


def _under_floor(path: str, floor_dirs: Iterable[str]) -> bool:
    return any(path == floor or path.startswith(f"{floor}/") for floor in floor_dirs)


@dataclass(frozen=True)
class SelectionSources:
    """The injected resolvers the pure core selects from — no tach, no disk, no git."""

    dependents_map: Mapping[str, list[str]]
    test_imports: Mapping[str, tuple[str, ...]]
    mirror_lookup: Callable[[str], str | None]
    doc_reader_lookup: Callable[[frozenset[str]], tuple[str, ...]]


class _Selected:
    """Accumulates test → reason, first pass wins, never selecting a floor-dir test."""

    def __init__(self, floor_dirs: Iterable[str]) -> None:
        self._floor_dirs = tuple(floor_dirs)
        self.reasons: dict[str, SelectionReason] = {}

    def add(self, test: str, kind: str, chain: tuple[str, ...]) -> bool:
        if test in self.reasons or _under_floor(test, self._floor_dirs):
            return False
        self.reasons[test] = SelectionReason(test=test, kind=kind, chain=chain)
        return True


def _add_import_matches(
    selected: _Selected, sources: SelectionSources, index: _ClosureIndex, parent: Mapping[str, str | None]
) -> None:
    for test in sorted(sources.test_imports):
        for imported in sources.test_imports[test]:
            closure_file = _match_closure_file(imported, index.module_to_file, index.prefix_to_file)
            if closure_file is not None:
                selected.add(test, "import-match", _import_chain(test, imported, closure_file, parent))
                break


def _add_mirrors(selected: _Selected, sources: SelectionSources, index: _ClosureIndex) -> list[str]:
    warnings: list[str] = []
    for module in sorted(index.module_to_file):
        mirror = sources.mirror_lookup(module)
        if mirror and selected.add(mirror, "mirror", (f"mirror path of {module}",)):
            warnings.append(f"mirror {mirror} for {module} not caught by the import scan — included belt-and-braces")
    return warnings


def select(
    *,
    changed: ChangedSet,
    sources: SelectionSources,
    floor_dirs: tuple[str, ...] = FLOOR_DIRS,
) -> Selection:
    """The pure selection core: classify, expand the reverse-import closure, match tests.

    Every input is injected via *sources* so the selection is deterministic and needs
    no tach/disk — the impure edges live in :func:`build_selection`.
    """
    verdict = classify_selection(changed)
    if verdict.full:
        return Selection(full=True, reason=verdict.reason, create_db=verdict.create_db, floor_dirs=floor_dirs)

    changed_src = tuple(str(p) for p in verdict.scoped_src)
    changed_tests = tuple(str(p) for p in verdict.scoped_tests)

    parent = dependents_closure(changed_src, sources.dependents_map)
    index = _closure_index(parent)

    selected = _Selected(floor_dirs)
    for test in changed_tests:
        selected.add(test, "self-changed", (f"{test} (changed test)",))
    _add_import_matches(selected, sources, index, parent)
    for reader in sources.doc_reader_lookup(reference_tokens(verdict.scoped_docs)):
        selected.add(reader, "doc-read", (f"{reader} reads a changed doc",))
    warnings = _add_mirrors(selected, sources, index)

    ordered = sorted(selected.reasons)
    return Selection(
        full=False,
        reason=verdict.reason or "scoped to the diff — no FULL trigger",
        create_db=verdict.create_db,
        test_files=tuple(ordered),
        floor_dirs=floor_dirs,
        doctest_targets=changed_src,
        reasons=tuple(selected.reasons[test] for test in ordered),
        changed_src=changed_src,
        changed_tests=changed_tests,
        changed_docs=verdict.scoped_docs,
        warnings=tuple(warnings),
    )


def _is_first_party(dotted: str) -> bool:
    return dotted == _FIRST_PARTY_ROOT or dotted.startswith(f"{_FIRST_PARTY_ROOT}.")


def first_party_imports_deep(source: str) -> tuple[str, ...]:
    """Every first-party ``teatree.*`` module imported ANYWHERE in *source*.

    Walks the full AST — not only module level, unlike the mirror gate's top-level
    scan — so a deferred / ``TYPE_CHECKING`` import of a changed module still selects
    the test. The no-under-select bias favours the deeper walk (more over-selection).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names if _is_first_party(alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and _is_first_party(node.module):
            modules.append(node.module)
    return tuple(dict.fromkeys(modules))


def _is_test_module(path: Path) -> bool:
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _resolve_tach() -> str | None:
    """The tach executable — on PATH, else beside the interpreter (a uv-tool venv bin)."""
    on_path = shutil.which("tach")
    if on_path is not None:
        return on_path
    adjacent = Path(sys.executable).parent / "tach"
    return str(adjacent) if adjacent.is_file() else None


def run_tach_dependents_map(root: Path) -> dict[str, list[str]]:
    """The ``tach map --direction dependents`` file-level reverse-adjacency, freshly built."""
    tach = _resolve_tach()
    if tach is None:
        message = "tach executable not found on PATH or beside the interpreter"
        raise TachUnavailableError(message)
    result = run_allowed_to_fail([tach, "map", "--direction", "dependents"], expected_codes=None, cwd=root)
    if result.returncode != 0:
        message = f"tach map failed ({result.returncode}): {result.stderr.strip()[:200]}"
        raise TachUnavailableError(message)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        message = f"tach map emitted invalid JSON: {exc}"
        raise TachUnavailableError(message) from exc


def scan_test_imports(root: Path) -> dict[str, tuple[str, ...]]:
    """Map every test file under ``tests/`` to its first-party imports (AST, no execution)."""
    imports: dict[str, tuple[str, ...]] = {}
    for path in collect_test_files(root):
        if not _is_test_module(path):
            continue
        modules = first_party_imports_deep(_read_text(path))
        if modules:
            imports[path.relative_to(root).as_posix()] = modules
    return imports


def disk_mirror_lookup(root: Path) -> Callable[[str], str | None]:
    """A mirror resolver: module → its ``tests/teatree_<pkg>/.../test_<leaf>.py`` if it exists."""

    def lookup(module: str) -> str | None:
        expected = expected_test_dir(module, root)
        if expected is None:
            return None
        leaf = module.rsplit(".", 1)[-1]
        candidate = f"{expected.path}/test_{leaf}.py"
        return candidate if (root / candidate).is_file() else None

    return lookup


def build_selection(root: Path, base_ref: str = "origin/main") -> Selection:
    """Wire the impure edges (git diff, tach map, disk scan) around the pure core.

    Fail-safe throughout: a dirty/shallow merge-base or an unavailable tach map both
    degrade to a whole-tree FULL run — a selector that cannot prove its selection must
    run everything, never skip-as-pass. tach is skipped entirely when the diff already
    classifies FULL, and when no src module changed at all (a docs-only diff has an
    empty reverse-import closure, so the map would cost seconds to answer nothing).
    """
    try:
        changed = changed_paths(base_ref=base_ref, cwd=root)
    except ChangedSetError as exc:
        return Selection(full=True, reason=f"could not compute the changed set ({exc}) — FULL (fail-safe)")

    verdict = classify_selection(changed)
    if verdict.full:
        return Selection(full=True, reason=verdict.reason, create_db=verdict.create_db)

    dependents_map: Mapping[str, list[str]] = {}
    if verdict.scoped_src:
        try:
            dependents_map = run_tach_dependents_map(root)
        except TachUnavailableError as exc:
            return Selection(
                full=True,
                reason=f"tach dependency map unavailable ({exc}) — FULL (fail-safe)",
                create_db=verdict.create_db,
            )

    return select(
        changed=changed,
        sources=SelectionSources(
            dependents_map=dependents_map,
            test_imports=scan_test_imports(root),
            mirror_lookup=disk_mirror_lookup(root),
            doc_reader_lookup=disk_doc_reader_lookup(root),
        ),
        floor_dirs=FLOOR_DIRS,
    )
