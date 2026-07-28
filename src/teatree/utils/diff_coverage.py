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

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_TEST_PATH_RE = re.compile(r"(^|/)(tests?/|test_|conftest\.py)")


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path))


def _is_production_python(path: str) -> bool:
    return path.endswith(".py") and not _is_test_path(path)


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
            rows.append(
                "  new production symbols not referenced by any changed test "
                f"(test-a-local-copy vacuity risk): {sorted(self.unreferenced_symbols)}"
            )
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
    those established patterns. Only files inside the coverage ``source``
    scope are considered — the symbol check matches the line-coverage
    check's file set.
    """
    added = added_lines_by_file(diff)
    out: dict[str, set[str]] = {}
    for path, lines in added.items():
        if not _is_production_python(path) or not scope.includes(path):
            continue
        source_file = repo_root / path
        if not source_file.is_file():
            continue
        try:
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
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
        if names:
            out[path] = names
    return out


@dataclass(frozen=True)
class _QualifiedReference:
    """One ``module.symbol`` access resolved back to the module it reads through.

    ``module_parts`` is the dotted module path with every alias
    substituted back to its ``import`` target, so ``import pkg.mod as m``
    + ``m.symbol`` and ``import pkg.mod`` + ``pkg.mod.symbol`` both
    resolve to ``("pkg", "mod")``.
    """

    module_parts: tuple[str, ...]
    symbol: str


@dataclass(frozen=True)
class _ReferenceScan:
    """How a changed test module can reach a production symbol.

    ``imported`` is every name an ``import``/``from … import`` binds (the
    alias if present). ``qualified`` is every ``module.symbol`` access
    read through a name an ``import`` statement bound to a module.
    ``shadowed`` is every name the test module itself ``def``/``class``-
    defines at any level. A local redefinition captures every *bare* call
    to that name, which makes the module ambiguous about which copy it is
    asserting on, so a shadowed symbol is refused outright — no import or
    qualified access cancels it.
    """

    imported: set[str]
    qualified: set[_QualifiedReference]
    shadowed: set[str]


def _attribute_chain(node: ast.Attribute) -> tuple[str, ...] | None:
    """The dotted chain of a ``Name``-rooted attribute access, root first.

    ``pkg.mod.symbol`` nests as ``Attribute(Attribute(Name('pkg')))``, so
    the chain is unwound to its base to recover the name an ``import``
    could have bound. A chain rooted in anything else (``self.symbol``,
    ``get_obj().symbol``, a subscript) has no import provenance and
    yields ``None``.
    """
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _module_alias_targets(node: ast.Import) -> dict[str, tuple[str, ...]]:
    """The ``{bound name: dotted module path}`` an ``import`` statement establishes.

    ``import pkg.mod`` binds only ``pkg``, and the access spells the rest
    out (``pkg.mod.symbol``), so the target is the root alone. ``import
    pkg.mod as m`` binds ``m`` to the whole path, which the access then
    omits — substituting the target for the root normalises both spellings
    to the same module path.
    """
    targets: dict[str, tuple[str, ...]] = {}
    for alias in node.names:
        root = alias.name.split(".")[0]
        targets[alias.asname or root] = tuple(alias.name.split(".")) if alias.asname else (root,)
    return targets


def _from_import_alias_targets(node: ast.ImportFrom) -> dict[str, tuple[str, ...]]:
    """The candidate ``{bound name: dotted module path}`` of a ``from … import``.

    ``from pkg import mod`` + ``mod.symbol()`` is the same qualified shape
    as ``import pkg.mod``, so the bound name is mapped to ``pkg.mod``.
    Whether the name is really a module is not knowable from the source,
    but it does not need to be: the provenance check only credits the
    access when that path matches the changed production file, which a
    class or function bound by the same syntax never does. Relative
    imports carry no resolvable package here and are skipped.
    """
    if not node.module or node.level:
        return {}
    package = tuple(node.module.split("."))
    return {alias.asname or alias.name: (*package, alias.name) for alias in node.names}


def _rebound_name(node: ast.AST) -> str | None:
    """The name *node* binds to something other than its import, if any.

    An assignment target, ``for`` target, ``with … as``, walrus, function
    parameter or ``except … as`` all rebind a bare name; once rebound, the
    name no longer denotes the module an ``import`` bound it to.
    """
    if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
        return node.id
    if isinstance(node, ast.arg):
        return node.arg
    if isinstance(node, ast.ExceptHandler):
        return node.name
    return None


def _scan_test_references(tree: ast.Module) -> _ReferenceScan:
    """Collect the import, qualified-access and shadowing names of a test module.

    Attribute accesses are resolved after the walk rather than inside it:
    an import nested in a function body can appear later in ``ast.walk``
    order than an attribute access it binds, so the alias map must be
    complete before any chain root is resolved against it.

    Three narrowing rules keep a qualified access a *production* read:

    - the chain root must resolve to a dotted module path. Both
    ``import pkg.mod`` and ``from pkg import mod`` are mapped, but the
    resulting path is only credited when
    :func:`_reads_production_module` matches it against the changed
    file, so ``from pathlib import Path`` + ``Path.cwd()`` — a class's
    namespace, not a module's — never satisfies a production ``cwd``.
    - only an ``ast.Load`` access reads the module. ``prod.symbol = fake``
    (``Store``) and ``del prod.symbol`` (``Del``) *replace* production;
    counting them as references would let the canonical monkeypatch-then-
    test-the-fake vacuity satisfy the gate.
    - a root rebound anywhere in the module — by assignment, ``for``
    target, ``with … as``, walrus, or a test-function parameter (a
    fixture shadowing the module name) — no longer denotes the imported
    module, so its accesses are dropped.
    """
    imported: set[str] = set()
    module_aliases: dict[str, tuple[str, ...]] = {}
    shadowed: set[str] = set()
    rebound: set[str] = set()
    attributes: list[ast.Attribute] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
            module_aliases.update(_from_import_alias_targets(node))
        elif isinstance(node, ast.Import):
            alias_targets = _module_alias_targets(node)
            module_aliases.update(alias_targets)
            imported.update(alias_targets)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            shadowed.add(node.name)
        elif isinstance(node, ast.Attribute):
            attributes.append(node)
        elif (name := _rebound_name(node)) is not None:
            rebound.add(name)

    live_aliases = {name: target for name, target in module_aliases.items() if name not in rebound}
    return _ReferenceScan(
        imported=imported,
        qualified={
            reference
            for node in attributes
            if (reference := _resolve_qualified_reference(node, live_aliases)) is not None
        },
        shadowed=shadowed,
    )


def _resolve_qualified_reference(
    node: ast.Attribute,
    module_aliases: dict[str, tuple[str, ...]],
) -> _QualifiedReference | None:
    """Resolve one attribute access to the module and symbol it reads, if any."""
    chain = _attribute_chain(node)
    if chain is None or not isinstance(node.ctx, ast.Load):
        return None
    alias_target = module_aliases.get(chain[0])
    if alias_target is None:
        return None
    resolved = alias_target + chain[1:]
    return _QualifiedReference(module_parts=resolved[:-1], symbol=resolved[-1])


def _module_parts_of(path: str) -> tuple[str, ...]:
    """The dotted module path a production file would be imported as.

    A package's ``__init__.py`` is imported as the package itself, so the
    trailing component is dropped for it.
    """
    parts = Path(path).with_suffix("").parts
    return parts[:-1] if parts and parts[-1] == "__init__" else parts


def _reads_production_module(production_path: str, module_parts: tuple[str, ...]) -> bool:
    """Whether a qualified access's module path denotes *this* production file.

    The access is written relative to whatever is importable at test time
    (``import classify_day`` for ``timely-log/scripts/classify_day.py``,
    ``import teatree.utils.diff_coverage`` for ``src/teatree/utils/
    diff_coverage.py``), so the repo-relative path is matched by suffix
    rather than resolved against ``sys.path``. That keeps a same-named
    symbol on an unrelated module — ``subprocess.run`` standing in for a
    changed production ``run`` — from satisfying the gate.
    """
    production_parts = _module_parts_of(production_path)
    return bool(module_parts) and production_parts[-len(module_parts) :] == module_parts


def unreferenced_changed_symbols(diff: str, repo_root: Path, scope: CoverageScope | None = None) -> set[str]:
    """Return changed production symbols NOT genuinely referenced by a test.

    The structural anti-vacuity check. For every symbol whose definition
    is added/changed in this diff, at least one test file *also changed in
    this diff* must reach that symbol through an import (so a revert of
    production turns the test red). A test that instead redefines a local
    copy of the symbol — the "test-a-local-copy" vacuity mechanism — never
    imports it; a bare textual call then resolves to the local copy, so it
    is not a genuine production reference and the symbol stays in the
    returned (failing) set.

    Two import shapes reach production:

    - ``from prod import symbol`` binds the symbol itself.
    - ``import prod`` + ``prod.symbol(...)`` reaches the symbol through
    the module object. Refusing this shape was the gate conflating "bare
    textual call" (genuinely ambiguous — it resolves to whatever is in
    scope) with "qualified attribute access on an imported module"
    (unambiguous — it reads through the module object).

    SHADOWING IS ABSOLUTE, and applies identically to both shapes: if the
    test module itself ``def``/``class``-defines the symbol's name, the
    symbol is never credited, however it is also referenced. A qualified
    reference does NOT cancel it. Letting one cancel the other is what
    made a single no-op smoke line launder a whole file —

        import prod
        def symbol(): return 0                        # the local copy
        def test_exists(): assert prod.symbol is not None
        def test_behaviour(): assert symbol() == 0    # asserts the COPY

    — because every real assertion binds to the local copy while the
    smoke line supplied the "reference". A test module that both ships
    its own copy of a symbol and reaches for production is ambiguous at
    every bare call site, so it is refused outright; splitting the local
    helper out under a different name is the fix.

    A qualified access is matched by import PROVENANCE: the accessed
    module path must be a suffix of the changed production file's path,
    so ``subprocess.run``, ``Path.cwd`` and ``pytest.raises`` cannot
    stand in for a changed production ``run``, ``cwd`` or ``raises``.
    The ``from … import`` shape remains module-BLIND — a same-named
    import from an unrelated module satisfies it — which is pre-existing
    behavior, narrowed separately.

    A qualified access need not be a ``Call``. ``prod.Enum.MEMBER`` and
    ``x: prod.Type`` read through the module object and go red when
    production is reverted, so they are genuine references; requiring a
    call would reject them. What stops an unexercised reference from
    passing the gate is the line-coverage half, not this one — a symbol
    that is referenced but never run keeps its body lines uncovered.

    Reachability is out of scope for a syntactic check: a qualified
    access inside ``if False:`` still counts here. That residual is
    covered on both flanks — shadowing refuses the local-copy case, and
    the line-coverage half refuses the never-executed case.

    This stays a *reference* check, never an exercise check. Catching
    "referenced-but-never-called" is the job of the line-coverage half of
    the gate (a referenced-but-uncalled symbol's body lines stay
    uncovered): the two halves are paired by design, not redundant. The
    reference check defeats the test-a-local-copy vacuity; the
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
    qualified_all: set[_QualifiedReference] = set()
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
        references = _scan_test_references(tree)
        imported_all |= references.imported
        qualified_all |= references.qualified
        shadowed_all |= references.shadowed

    referenced = {s for s in all_symbols if s in imported_all and s not in shadowed_all}
    for path, symbols in changed.items():
        referenced |= {
            reference.symbol
            for reference in qualified_all
            if reference.symbol in symbols
            and _reads_production_module(path, reference.module_parts)
            and reference.symbol not in shadowed_all
        }
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


def _uncovered_via_fresh_analysis(cov: object, abs_path: str, added: set[int]) -> list[int]:
    """Executable added lines for a file coverage never imported.

    ``coverage.analysis2`` still parses an *un-measured* source file and
    reports its executable lines; with no measured data every executable
    line is missing, so the intersection with the diff's added lines is
    exactly the uncovered new lines.
    """
    try:
        _, executable, _, missing, _ = cov.analysis2(abs_path)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — coverage raises various NoSource/CoverageException types
        return sorted(added)
    return sorted(added & set(executable) & set(missing))
