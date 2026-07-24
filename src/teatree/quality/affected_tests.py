"""Safety-biased incremental test selection (#113, #3672 cutover).

Fast-feedback ONLY. The whole-tree sharded run stays the merge/coverage gate; this
selector is opt-in local tooling and is NEVER wired into the pre-push gate.

The impact engine is the tach pytest plugin (`--tach --tach-base origin/main`): it
walks the import graph natively and deselects the tests a diff cannot reach. This
module no longer re-implements that graph walk — the hand-rolled subprocess and the
graph-adjacency parsing it used to maintain are gone (#3672).

What survives is the ESCALATION policy, a safety contract the plugin cannot infer:

- ``classify_selection`` — force-FULL on conftest / factories / test settings /
migrations / any unclassifiable executable change, and ``--create-db`` on a migration.
A FULL verdict runs the whole suite with the plugin OFF, so nothing is deselected.
- ``build_force_keep`` — on a SCOPED verdict, the floor dirs, the doc-reader mapping,
the test-path-mirror rule, and the changed test files themselves become a FORCE-KEEP
layer applied over the plugin's deselection by ``teatree.quality.force_keep_plugin``,
in the SAME pytest session. Zero test runs twice.

Under-run is a false green — the same doctrine as :mod:`teatree.quality.changed_set`,
the shared changed-set + FULL-trigger normalizer this builds on. Over-run is not free
either (#3645): a one-module fix escalated to 30182 tests over 59m32s because the diff
carried the ``BLUEPRINT.md`` edit the blueprint-sync gate compels. Docs are therefore
classified rather than blanket-escalated (:mod:`teatree.quality.doc_impact`) and mapped
to the tests that READ them; the escalation stays exactly as conservative for anything
executable.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.changed_set import ChangedSet, ChangedSetError, FullTrigger, changed_paths, classify, is_migration
from teatree.quality.doc_impact import disk_doc_reader_lookup, is_doc_path, reference_tokens
from teatree.quality.test_path_mirror import expected_test_dir

#: The tach pytest plugin's default base — our escalation diff pins the same ref, so the
#: force-keep layer and the plugin agree on which commits count as "changed".
DEFAULT_BASE = "origin/main"

#: The dotted import path pytest loads the force-keep layer from (``-p <this>``).
FORCE_KEEP_PLUGIN = "teatree.quality.force_keep_plugin"

#: Cross-cutting, subprocess-heavy suites an import graph cannot fully model — force-keep
#: them on EVERY scoped selection so their blind spot is a constant cost, not a skip.
FLOOR_DIRS: tuple[str, ...] = ("tests/quality", "tests/integration", "tests/conformance")

_SRC_MODULE_PREFIX = "src/teatree/"
_SRC_PREFIX = "src/"
_TESTS_PREFIX = "tests/"
_TESTS_CONFIG_PREFIX = "tests/config/"


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
    kind: str  # floor | doc-read | mirror | self-changed
    chain: tuple[str, ...]


@dataclass(frozen=True)
class ForceKeep:
    """The escalation set force-kept over the tach plugin's deselection (SCOPED only)."""

    paths: tuple[str, ...] = ()
    reasons: tuple[SelectionReason, ...] = ()
    warnings: tuple[str, ...] = ()

    def covers(self, rel_path: str) -> bool:
        """True when *rel_path* is force-kept — an exact match or under a kept directory."""
        return any(rel_path == kept or rel_path.startswith(f"{kept}/") for kept in self.paths)


@dataclass(frozen=True)
class Selection:
    full: bool
    reason: str
    create_db: bool = False
    base_ref: str = DEFAULT_BASE
    force_keep: tuple[str, ...] = ()
    doctest_targets: tuple[str, ...] = ()
    reasons: tuple[SelectionReason, ...] = ()
    changed_src: tuple[str, ...] = ()
    changed_tests: tuple[str, ...] = ()
    changed_docs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def pytest_args(self, *, test_db_cloned: bool = False) -> list[str]:
        """Positional pytest args for this selection.

        A FULL verdict emits at most a DB flag and runs the whole suite with the plugin
        OFF. A SCOPED verdict activates the tach plugin (``--tach --tach-base <base>``)
        and loads the force-keep layer (``-p teatree.quality.force_keep_plugin``); the
        two run in ONE session. Changed src modules keep CI's ``--doctest-modules``
        parity — passing them (and an explicit ``tests`` root, so the positionals do not
        clobber ``testpaths``) as collection roots.

        ``create_db`` normally emits ``--create-db``; when the caller has refreshed the
        test DB via the opt-in template clone (souliane/teatree#3326), pass
        ``test_db_cloned=True`` so the same drift instead emits ``--reuse-db``.
        """
        db = (["--reuse-db"] if test_db_cloned else ["--create-db"]) if self.create_db else []
        if self.full:
            return db  # plugin OFF ⇒ the runner executes the whole suite
        plugin = ["--tach", "--tach-base", self.base_ref, "-p", FORCE_KEEP_PLUGIN]
        if self.doctest_targets:
            return [*db, *plugin, _TESTS_PREFIX.rstrip("/"), *self.doctest_targets, "--doctest-modules"]
        return [*db, *plugin]

    def report(self) -> str:
        if self.full:
            return f"affected-tests: FULL — {self.reason}"
        return (
            f"affected-tests: SCOPED (tach plugin + force-keep) — {len(self.force_keep)} force-kept path(s), "
            f"{len(self.doctest_targets)} changed src module(s), {len(self.changed_docs)} changed doc(s)"
        )

    def explain(self, test: str | None = None) -> list[str]:
        chosen = [reason for reason in self.reasons if test is None or reason.test == test]
        if test is not None and not chosen:
            return [f"{test}: not force-kept by this diff (tach selects it only if a changed module reaches it)"]
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


def _under_floor(path: str, floor_dirs: tuple[str, ...]) -> bool:
    return any(path == floor or path.startswith(f"{floor}/") for floor in floor_dirs)


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


class _ForceKept:
    """Accumulates path → reason, first pass wins, never listing a floor-dir test twice."""

    def __init__(self, floor_dirs: tuple[str, ...]) -> None:
        self._floor_dirs = floor_dirs
        self.reasons: dict[str, SelectionReason] = {}

    def add(self, path: str, kind: str, chain: tuple[str, ...]) -> bool:
        if path in self.reasons or _under_floor(path, self._floor_dirs):
            return False
        self.reasons[path] = SelectionReason(test=path, kind=kind, chain=chain)
        return True


def build_force_keep(
    root: Path,
    verdict: SelectionVerdict,
    *,
    floor_dirs: tuple[str, ...] = FLOOR_DIRS,
    doc_reader_lookup: Callable[[frozenset[str]], tuple[str, ...]] | None = None,
    mirror_lookup: Callable[[str], str | None] | None = None,
) -> ForceKeep:
    """The escalation set force-kept over the tach plugin, computed from *verdict*.

    A FULL verdict never runs the plugin, so its force-keep is irrelevant → empty. A
    SCOPED verdict force-keeps the floor dirs (always), the changed test files, the
    doc-reader tests for any changed doc, and the mirror test of each changed src module
    (belt-and-braces: the plugin's import walk should already reach it). The disk
    resolvers are injected so the pure assembly is testable without disk.
    """
    if verdict.full:
        return ForceKeep()

    reader_lookup = doc_reader_lookup if doc_reader_lookup is not None else disk_doc_reader_lookup(root)
    mirror = mirror_lookup if mirror_lookup is not None else disk_mirror_lookup(root)

    kept = _ForceKept(floor_dirs)
    warnings: list[str] = []

    for floor in floor_dirs:
        kept.reasons[floor] = SelectionReason(test=floor, kind="floor", chain=(f"{floor} always runs (cross-cutting)",))

    for changed_test in (str(p) for p in verdict.scoped_tests):
        kept.add(changed_test, "self-changed", (f"{changed_test} (changed test)",))

    for reader in reader_lookup(reference_tokens(verdict.scoped_docs)):
        kept.add(reader, "doc-read", (f"{reader} reads a changed doc",))

    for module in (module_of(str(p)) for p in verdict.scoped_src):
        if module is None:
            continue
        mirror_path = mirror(module)
        if mirror_path and kept.add(mirror_path, "mirror", (f"mirror path of {module}",)):
            warnings.append(f"mirror {mirror_path} for {module} force-kept over tach — belt-and-braces")

    ordered = sorted(kept.reasons)
    return ForceKeep(
        paths=tuple(ordered),
        reasons=tuple(kept.reasons[path] for path in ordered),
        warnings=tuple(warnings),
    )


def build_selection(root: Path, base_ref: str = DEFAULT_BASE) -> Selection:
    """Wire the impure edge (git diff) around the escalation policy — NO tach subprocess.

    Fail-safe: a dirty/shallow merge-base degrades to a whole-tree FULL run with the
    plugin OFF — a selector that cannot prove its scope must run everything, never
    skip-as-pass. On a scoped verdict the tach plugin does the reverse-import graph walk
    in-session; this returns only the escalation force-keep layer over it.
    """
    try:
        changed = changed_paths(base_ref=base_ref, cwd=root)
    except ChangedSetError as exc:
        return Selection(full=True, reason=f"could not compute the changed set ({exc}) — FULL (fail-safe)")

    verdict = classify_selection(changed)
    if verdict.full:
        return Selection(full=True, reason=verdict.reason, create_db=verdict.create_db, base_ref=base_ref)

    force_keep = build_force_keep(root, verdict)
    changed_src = tuple(str(p) for p in verdict.scoped_src)
    return Selection(
        full=False,
        reason=verdict.reason or "scoped to the diff — no FULL trigger",
        create_db=verdict.create_db,
        base_ref=base_ref,
        force_keep=force_keep.paths,
        doctest_targets=changed_src,
        reasons=force_keep.reasons,
        changed_src=changed_src,
        changed_tests=tuple(str(p) for p in verdict.scoped_tests),
        changed_docs=verdict.scoped_docs,
        warnings=force_keep.warnings,
    )
