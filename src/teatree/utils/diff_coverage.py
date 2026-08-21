"""Per-diff coverage + mutation/revert structural gate.

BLUEPRINT §17.6 gate 12 (#836). The project-wide ``fail_under`` floor
says nothing about a diff's *own* new lines: WS5 / #776 / #800 shipped
false "100% coverage" / "anti-vacuous" claims because a global
percentage can stay green while the newly-added high-value lines are
entirely untested. This module measures coverage on the diff's
added/changed *production* lines and fails if any are uncovered.

It also runs a structural mutation/revert check. A coverage gate alone
cannot catch the "test-a-local-copy" vacuity mechanism: a test that
redefines the production logic inside the test file and never imports
the shipped symbol can show "100%" while asserting nothing about
production — reverting production would not turn it red. The structural
check requires every new/changed production symbol to be *referenced by
name* from a test file changed in the same diff.

The two checks are combined into a single :class:`DiffCoverageReport`
the CLI and the pre-merge hook gate act on. Exit non-zero ⇒ the PR is
returned to draft (§17.6.3 gate placement).
"""

import ast
import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coverage import Coverage

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_TEST_PATH_RE = re.compile(r"(^|/)(tests?/|test_|conftest\.py)")


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


def _is_production_python(path: str) -> bool:
    return path.endswith(".py") and not _is_test_path(path)


def _is_framework_migration(path: str) -> bool:
    """Whether *path* is a Django migration module.

    Migrations are framework-loaded by name and are conventionally numbered
    (``0406_add_….py``), so the module name is not a Python identifier and
    ``from <package>.0406_add_… import Migration`` is a ``SyntaxError``. The
    symbol check's remedy — make the reference visible with an import — is
    therefore unavailable to every migration, so its ``Migration`` class is
    exempted for the same reason as a decorated framework entrypoint: it is
    exercised through the framework (``migrate``, and the migration tests that
    invoke the ``RunPython`` callables), never by importing the class by name.
    """
    return "/migrations/" in f"/{path.lstrip('/')}"


@dataclass(frozen=True)
class CoverageScope:
    """The ``[tool.coverage.run]`` ``source`` roots and ``omit`` globs.

    The per-diff gate measures exactly the file set the project's own
    coverage config measures — files outside ``source`` (e.g. ``scripts/``
    or ``hooks/`` invoked only as subprocesses, the established
    ``privacy_scan.py`` pattern) are out of scope for *line* coverage,
    just as they are for the existing global ``fail_under`` gate. This
    keeps the gate aligned with §17.6's target (untested high-value NEW
    lines in measured source) rather than demanding impossible coverage
    of subprocess-only scripts.
    """

    source_roots: tuple[str, ...]
    omit: tuple[str, ...]

    def includes(self, repo_relative_path: str) -> bool:
        if not self.source_roots:
            return True
        in_source = any(
            repo_relative_path == root or repo_relative_path.startswith(f"{root.rstrip('/')}/")
            for root in self.source_roots
        )
        if not in_source:
            return False
        return not any(fnmatch.fnmatch(repo_relative_path, pattern) for pattern in self.omit)


def load_coverage_scope(pyproject_path: Path) -> CoverageScope:
    """Read ``[tool.coverage.run]`` ``source``/``omit`` from pyproject.

    Missing config ⇒ an empty-roots scope that includes everything (the
    gate degrades to "all production python", never silently to a no-op).
    """
    if not pyproject_path.is_file():
        return CoverageScope(source_roots=(), omit=())
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    run_cfg = data.get("tool", {}).get("coverage", {}).get("run", {})
    return CoverageScope(
        source_roots=tuple(run_cfg.get("source", [])),
        omit=tuple(run_cfg.get("omit", [])),
    )


def added_lines_by_file(diff: str) -> dict[str, set[int]]:
    """Map each file to the set of line numbers it *adds* in the new file.

    Parses unified-diff hunk headers (``@@ -a,b +c,d @@``) and walks the
    body, tracking the resulting-file line counter so each ``+`` line is
    recorded at its post-image line number. Removed/context lines do not
    advance into the added set.
    """
    result: dict[str, set[int]] = {}
    current: str | None = None
    new_lineno = 0
    for line in diff.splitlines():
        file_match = _DIFF_FILE_RE.match(line)
        if file_match:
            current = file_match.group(1)
            result.setdefault(current, set())
            continue
        hunk_match = _HUNK_RE.match(line)
        if hunk_match:
            new_lineno = int(hunk_match.group(1))
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result[current].add(new_lineno)
            new_lineno += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif not line.startswith("\\"):
            new_lineno += 1
    return {f: lines for f, lines in result.items() if lines}


@dataclass(frozen=True)
class UncoveredFile:
    path: str
    lines: list[int]


UNREFERENCED_SYMBOL_IMPORT_HINT = (
    "    workaround: this check reads a changed test's import statements only — a "
    "`module.symbol(...)` call does not count as a reference; when the symbol is already "
    "exercised, add `from module import symbol` to a changed test to make the reference visible"
)


@dataclass(frozen=True)
class DiffCoverageReport:
    uncovered: list[UncoveredFile] = field(default_factory=list)
    unreferenced_symbols: list[str] = field(default_factory=list)

    def passes(self) -> bool:
        return not self.uncovered and not self.unreferenced_symbols

    def summary(self) -> str:
        if self.passes():
            return "Per-diff coverage gate: clean (all new lines covered, symbols referenced)"
        rows: list[str] = ["Per-diff coverage gate: FAILED"]
        rows.extend(f"  uncovered new lines in {uf.path}: {uf.lines}" for uf in self.uncovered)
        if self.unreferenced_symbols:
            symbol_row = (
                "  new production symbols not referenced by any changed test "
                f"(test-a-local-copy vacuity risk): {sorted(self.unreferenced_symbols)}"
            )
            rows.extend((symbol_row, UNREFERENCED_SYMBOL_IMPORT_HINT))
        return "\n".join(rows)


def _typing_protocol_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return ``(protocol_names, typing_module_aliases)`` bound to ``typing.Protocol``.

    ``protocol_names`` is every local name that ``from typing import
    Protocol [as X]`` binds to ``typing.Protocol`` directly.
    ``typing_module_aliases`` is every local name that ``import typing [as
    X]`` binds, so an ``<alias>.Protocol`` attribute access can be resolved
    back to ``typing.Protocol``. A same-named symbol imported from anywhere
    else (``from custom import Protocol``, ``class Foo(custom.Protocol)``)
    binds neither set, so :func:`_inherits_protocol` correctly refuses it —
    a bare name/attribute match with no import-provenance check would
    wrongly exempt an unrelated class that merely happens to be named
    ``Protocol`` (souliane/teatree#2888 review findings).

    Two scoping rules, both closing a review-found gap:

    - **Module level only** (``tree.body``, not :func:`ast.walk`): an
    ``import``/``from … import`` nested inside a function or class body is
    not visible at the module's top level where a class base is resolved,
    so it must not bind these sets.
    - **Last import wins, in source order**: imports are walked in the order
    they appear, and any later import of the *same local name* from a
    different origin (``from typing import Protocol`` then later ``from
    custom import Protocol``, or ``import typing as t`` then later ``import
    custom as t``) removes the earlier binding — the name no longer resolves
    to ``typing.Protocol`` at any later point in the file, exactly as
    Python's own name resolution rebinds it. A non-import rebinding (a plain
    assignment or a ``def``/``class`` redefining the same name) is not
    tracked — that shape already trips ruff's redefinition lint (``F811``),
    a mandatory gate, so it is out of scope here.
    """
    protocol_names: set[str] = set()
    typing_aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module == "typing" and alias.name == "Protocol":
                    protocol_names.add(bound)
                else:
                    protocol_names.discard(bound)
                typing_aliases.discard(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name == "typing":
                    typing_aliases.add(bound)
                else:
                    typing_aliases.discard(bound)
                protocol_names.discard(bound)
    return protocol_names, typing_aliases


def _inherits_protocol(node: ast.ClassDef, protocol_names: set[str], typing_aliases: set[str]) -> bool:
    """Whether *node* directly subclasses ``typing.Protocol`` (either import form).

    A source-level heuristic (no cross-file type resolution, matching the
    rest of this module): matches a base name bound by ``from typing import
    Protocol`` (``protocol_names``) or an attribute access whose object is
    bound by ``import typing`` (``typing_aliases``). A Protocol subclassing
    another *custom* Protocol base (not itself importing from ``typing``) is
    not detected — narrower is the safe default for a gate exemption.
    """
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in protocol_names:
            return True
        if (
            isinstance(base, ast.Attribute)
            and base.attr == "Protocol"
            and isinstance(base.value, ast.Name)
            and base.value.id in typing_aliases
        ):
            return True
    return False


def _public_symbols_on_lines(tree: ast.Module, lines: set[int]) -> set[str]:
    """Public module-level ``def``/``class`` names declared on *lines*.

    Private ``_``-prefixed helpers, decorated framework entrypoints and
    ``typing.Protocol`` classes are skipped — see
    :func:`_changed_production_symbols` for why each is exempt.
    """
    protocol_names, typing_aliases = _typing_protocol_bindings(tree)
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if node.lineno not in lines or node.name.startswith("_") or node.decorator_list:
            continue
        if isinstance(node, ast.ClassDef) and _inherits_protocol(node, protocol_names, typing_aliases):
            continue
        names.add(node.name)
    return names


def _changed_production_symbols(diff: str, repo_root: Path, scope: CoverageScope) -> dict[str, set[str]]:
    """Return ``{file_path: {public top-level symbols defined on added lines}}``.

    A symbol is "changed" when its **module-level** ``def``/``class``
    statement falls on a line the diff adds. The mutation/revert check
    targets the importable public API surface a regression test must
    call (§17.6): private ``_``-prefixed helpers are exercised through
    their public callers, framework-registered entrypoints (a
    ``@…command``/route-decorated callback) are tested through the
    framework, not by importing the callback by name, and a ``typing.
    Protocol`` class (souliane/teatree#2888) is a structural type contract
    with no revertible runtime behavior of its own — its conformance is
    checked by the type checker (``ty``/mypy) against each concrete
    implementation, not by a test importing the Protocol by name. Requiring
    that import produced the ad-hoc ``test_concrete_impls_satisfy_the_
    harness_protocols`` binding test in ``tests/teatree_agents/
    test_harness.py`` (#2565/#2885) purely to appease this check; this
    exemption generalizes that fix into the gate itself. So decorated
    top-level defs and Protocol classes are excluded to avoid penalising
    those established patterns. Django migrations are excluded for the same
    reason, with an extra twist that makes the exemption mandatory rather than
    merely kind: their module names are numbered, so the remedy this check
    prescribes cannot be written at all (see :func:`_is_framework_migration`).
    Only files inside the coverage ``source`` scope are considered — the symbol
    check matches the line-coverage check's file set.
    """
    added = added_lines_by_file(diff)
    out: dict[str, set[str]] = {}
    for path, lines in added.items():
        if not _is_production_python(path) or not scope.includes(path):
            continue
        if _is_framework_migration(path):
            continue
        source_file = repo_root / path
        if not source_file.is_file():
            continue
        try:
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        names = _public_symbols_on_lines(tree, lines)
        if names:
            out[path] = names
    return out


def _attribute_chain_root(node: ast.Attribute) -> str | None:
    """The root ``Name`` of an attribute chain (``a.b.c`` → ``a``), if it has one.

    A chain rooted in a call or subscript (``factory().attr``) has no static
    root name and returns ``None``.
    """
    current: ast.expr = node.value
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def _test_imported_and_shadowed(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return ``(referenced_names, locally_defined_names)`` for a test module.

    ``referenced_names`` is every production name the test module reaches, by
    any of the three idioms that resolve to production at call time — so a
    revert of production turns the test red:

    - ``from mod import symbol`` — the bound name;
    - ``from mod import symbol as alias`` — BOTH the alias and the underlying
    ``symbol``, since the alias is only a local spelling of the same object.
    Reading the bound name alone hid the symbol and reported a thoroughly
    exercised function as unreferenced;
    - ``import mod`` (aliased or not) plus ``mod.symbol`` attribute access —
    the idiom this codebase's tests actually use. The attribute name counts
    only when its chain is rooted in an imported MODULE binding, so a local
    stub's ``stub.symbol(...)`` is not mistaken for a production reference.

    ``locally_defined_names`` is every name the test module itself
    ``def``/``class``-defines at any level — a local redefinition that shadows
    the production symbol. That is the "test-a-local-copy" vacuity this check
    exists to catch, and shadowing still wins over every idiom above.
    """
    imported: set[str] = set()
    module_bindings: set[str] = set()
    attribute_uses: list[ast.Attribute] = []
    local: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = (alias.asname or alias.name).split(".")[0]
                imported.add(bound)
                module_bindings.add(bound)
        elif isinstance(node, ast.Attribute):
            attribute_uses.append(node)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            local.add(node.name)

    imported.update(
        attribute.attr for attribute in attribute_uses if _attribute_chain_root(attribute) in module_bindings
    )
    return imported, local


def unreferenced_changed_symbols(diff: str, repo_root: Path, scope: CoverageScope | None = None) -> set[str]:
    """Return changed production symbols NOT genuinely referenced by a test.

    The structural anti-vacuity check. For every symbol whose definition
    is added/changed in this diff, at least one test file *also changed in
    this diff* must **import** that symbol (so a revert of production turns
    the test red). A test that instead redefines a local copy of the
    symbol — the "test-a-local-copy" vacuity mechanism — never imports it;
    a bare textual call then resolves to the local copy, so it is not a
    genuine production reference and the symbol stays in the returned
    (failing) set. A symbol that is both imported and locally shadowed is
    treated as shadowed (the local def wins at call sites).

    This is deliberately only an *import* check. Catching
    "imported-but-never-called" is the job of the line-coverage half of
    the gate (an imported-but-uncalled symbol's body lines stay
    uncovered): the two halves are paired by design, not redundant. The
    import check defeats the test-a-local-copy vacuity; the
    line-coverage check defeats the import-without-exercise vacuity.
    Neither half alone is sufficient, which is why
    :func:`measure_diff_coverage` always runs both.
    """
    if scope is None:
        scope = load_coverage_scope(repo_root / "pyproject.toml")
    changed = _changed_production_symbols(diff, repo_root, scope)
    all_symbols: set[str] = set()
    for names in changed.values():
        all_symbols |= names
    if not all_symbols:
        return set()

    imported_all: set[str] = set()
    shadowed_all: set[str] = set()
    for path in added_lines_by_file(diff):
        if not (path.endswith(".py") and _is_test_path(path)):
            continue
        test_file = repo_root / path
        if not test_file.is_file():
            continue
        try:
            tree = ast.parse(test_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        imported, local = _test_imported_and_shadowed(tree)
        imported_all |= imported
        shadowed_all |= local

    referenced = {s for s in all_symbols if s in imported_all and s not in shadowed_all}
    return all_symbols - referenced


def measure_diff_coverage(
    diff: str,
    coverage_data_file: Path,
    repo_root: Path,
    scope: CoverageScope | None = None,
) -> DiffCoverageReport:
    """Build the combined per-diff coverage + mutation/revert report.

    For each production ``.py`` file the diff touches *that the project's
    coverage config measures* (``[tool.coverage.run] source``/``omit``),
    the set of added line numbers is intersected with the file's
    *missing* (uncovered) executable lines from the ``.coverage`` data.
    Any added line that is both executable and uncovered is a finding.
    The structural symbol check is folded in via
    :func:`unreferenced_changed_symbols`.
    """
    if scope is None:
        scope = load_coverage_scope(repo_root / "pyproject.toml")
    added = added_lines_by_file(diff)
    uncovered: list[UncoveredFile] = []

    if coverage_data_file.exists():
        import coverage  # noqa: PLC0415 — heavy import, only when a .coverage exists

        cov = coverage.Coverage(data_file=str(coverage_data_file))
        cov.load()
        measured = {Path(f).resolve(): f for f in cov.get_data().measured_files()}
        for path, lines in added.items():
            if not _is_production_python(path) or not scope.includes(path):
                continue
            resolved = (repo_root / path).resolve()
            actual = measured.get(resolved)
            if actual is None:
                # File never imported under coverage at all — every
                # executable added line is uncovered. Use the source to
                # find executable lines via a fresh analysis.
                missing_added = _uncovered_via_fresh_analysis(cov, str(resolved), lines)
            else:
                _, executable, _, missing, _ = cov.analysis2(actual)
                executable_set = set(executable)
                missing_added = sorted(lines & executable_set & set(missing))
            if missing_added:
                uncovered.append(UncoveredFile(path=path, lines=missing_added))

    unreferenced = sorted(unreferenced_changed_symbols(diff, repo_root, scope))
    return DiffCoverageReport(uncovered=uncovered, unreferenced_symbols=unreferenced)


def _uncovered_via_fresh_analysis(cov: "Coverage", abs_path: str, added: set[int]) -> list[int]:
    """Executable added lines for a file coverage never imported.

    ``coverage.analysis2`` still parses an *un-measured* source file and
    reports its executable lines; with no measured data every executable
    line is missing, so the intersection with the diff's added lines is
    exactly the uncovered new lines.
    """
    try:
        _, executable, _, missing, _ = cov.analysis2(abs_path)
    except Exception:  # noqa: BLE001 — coverage raises various NoSource/CoverageException types
        return sorted(added)
    return sorted(added & set(executable) & set(missing))
