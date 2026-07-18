"""Safety-biased incremental test selection (#113).

Fast-feedback ONLY. The whole-tree sharded run stays the merge/coverage gate; this
selector is opt-in local tooling and is NEVER wired into the pre-push gate.

Given a diff, decide which pytest test files to run. A changed ``src/teatree/**``
module expands to its transitive dependents — the reverse-import closure from
``tach map --direction dependents`` — and every test whose first-party imports hit
any module in that closure is selected, unioned with the mirror-convention test path
and an always-run floor. ANY change the classifier cannot prove local (conftest,
settings, migrations, non-``.py`` data files, deletions/renames, files outside the
modelled roots) degrades to a whole-tree FULL run. Over-run is free; under-run is a
false green — the same doctrine as :mod:`teatree.quality.changed_set`, the shared
changed-set + FULL-trigger normalizer this builds on.
"""

import ast
import json
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.changed_set import ChangedSet, ChangedSetError, FullTrigger, changed_paths, classify, is_migration
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
            f"{len(self.floor_dirs)} floor dir(s), {len(self.doctest_targets)} changed src module(s); "
            "full-run triggers: none"
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
    """Route a diff to FULL or the scoped src+test lists, reusing the shared classifier.

    Adds the #113-only escalations on top of :func:`teatree.quality.changed_set.classify`:
    factories/settings force FULL, a migration additionally requires ``--create-db``,
    and a file the shared classifier merely IGNORED (doc/markdown outside the code
    roots) becomes FULL — a doc/skill-parsing test may read it, so scoping it away
    would risk an under-select.
    """
    base: FullTrigger = classify(changed)
    create_db = any(is_migration(path) for path in changed.paths)
    if base.full:
        return SelectionVerdict(full=True, reason=base.reason, create_db=create_db)

    for entry in changed.entries:
        extra = _extra_full_trigger(entry.path)
        if extra:
            return SelectionVerdict(full=True, reason=f"{extra} ({entry.path})", create_db=create_db)

    scoped = {str(p) for p in base.scoped_src} | {str(p) for p in base.scoped_tests}
    ignored = [path for path in changed.paths if path not in scoped]
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


def _closure_index(parent: Mapping[str, str | None]) -> tuple[dict[str, str], dict[str, str]]:
    """Index the closure files by exact module and by every ancestor prefix."""
    module_to_file: dict[str, str] = {}
    prefix_to_file: dict[str, str] = {}
    for file in sorted(parent):
        module = module_of(file)
        if module is None:
            continue
        module_to_file.setdefault(module, file)
        for prefix in _prefixes(module):
            prefix_to_file.setdefault(prefix, file)
    return module_to_file, prefix_to_file


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


def select(
    *,
    changed: ChangedSet,
    dependents_map: Mapping[str, list[str]],
    test_imports: Mapping[str, tuple[str, ...]],
    mirror_lookup: Callable[[str], str | None],
    floor_dirs: tuple[str, ...] = FLOOR_DIRS,
) -> Selection:
    """The pure selection core: classify, expand the reverse-import closure, match tests.

    Every input is injected (the changed set, the dependents adjacency, the
    test→imports map, the mirror resolver) so the selection is deterministic and needs
    no tach/disk — the impure edges live in :func:`build_selection`.
    """
    verdict = classify_selection(changed)
    if verdict.full:
        return Selection(full=True, reason=verdict.reason, create_db=verdict.create_db, floor_dirs=floor_dirs)

    changed_src = tuple(str(p) for p in verdict.scoped_src)
    changed_tests = tuple(str(p) for p in verdict.scoped_tests)

    parent = dependents_closure(changed_src, dependents_map)
    module_to_file, prefix_to_file = _closure_index(parent)

    selected: dict[str, SelectionReason] = {}

    for test in changed_tests:
        if not _under_floor(test, floor_dirs):
            selected[test] = SelectionReason(test=test, kind="self-changed", chain=(f"{test} (changed test)",))

    for test in sorted(test_imports):
        if test in selected or _under_floor(test, floor_dirs):
            continue
        for imported in test_imports[test]:
            closure_file = _match_closure_file(imported, module_to_file, prefix_to_file)
            if closure_file is not None:
                selected[test] = SelectionReason(
                    test=test, kind="import-match", chain=_import_chain(test, imported, closure_file, parent)
                )
                break

    warnings: list[str] = []
    for module in sorted(module_to_file):
        mirror = mirror_lookup(module)
        if mirror and mirror not in selected and not _under_floor(mirror, floor_dirs):
            selected[mirror] = SelectionReason(test=mirror, kind="mirror", chain=(f"mirror path of {module}",))
            warnings.append(f"mirror {mirror} for {module} not caught by the import scan — included belt-and-braces")

    ordered = sorted(selected)
    return Selection(
        full=False,
        reason=verdict.reason or "scoped to the diff — no FULL trigger",
        create_db=verdict.create_db,
        test_files=tuple(ordered),
        floor_dirs=floor_dirs,
        doctest_targets=changed_src,
        reasons=tuple(selected[test] for test in ordered),
        changed_src=changed_src,
        changed_tests=changed_tests,
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


def run_tach_dependents_map(root: Path) -> dict[str, list[str]]:
    """The ``tach map --direction dependents`` file-level reverse-adjacency, freshly built."""
    result = run_allowed_to_fail(["tach", "map", "--direction", "dependents"], expected_codes=None, cwd=root)
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
    classifies FULL.
    """
    try:
        changed = changed_paths(base_ref=base_ref, cwd=root)
    except ChangedSetError as exc:
        return Selection(full=True, reason=f"could not compute the changed set ({exc}) — FULL (fail-safe)")

    verdict = classify_selection(changed)
    if verdict.full:
        return Selection(full=True, reason=verdict.reason, create_db=verdict.create_db)

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
        dependents_map=dependents_map,
        test_imports=scan_test_imports(root),
        mirror_lookup=disk_mirror_lookup(root),
        floor_dirs=FLOOR_DIRS,
    )
