"""Forward-guard: test files mirror their ``src/teatree/<pkg>/...`` module path.

The repo bar (``CLAUDE.md`` + ``/ac-python``): *tests mirror production code* —
a test for ``src/teatree/<pkg>/<sub>/foo.py`` lives at
``tests/teatree_<pkg>/<sub>/test_foo.py``. The convention is that the top-level
``teatree_`` prefix replaces ``src/teatree/`` and intermediate directories
mirror exactly. ~205 existing files predate the convention (loose at the
``tests/`` root, or mis-pathed across packages); this gate freezes that floor so
the upcoming relocation sweep can only ever shrink it, never regress.

The checker is pure-AST: for a test file it parses its top-level first-party
imports (``from teatree.<dotted> import ...`` / ``import teatree.<dotted>``)
WITHOUT executing them, maps each imported ``teatree.<pkg>...`` module to the
test directory the convention expects, and asks whether the file's ACTUAL
directory is a descendant-or-equal of any expected directory (i.e. an expected
dir CONTAINS it). A file MIRRORS iff at least one imported module's expected dir
contains it; otherwise it is a VIOLATION (the two dominant shapes:
loose-at-``tests/``-root, and mis-pathed cross-package). The one exception is a
top-level src MODULE (not a package): its expectation is the ``tests`` root, and
that demands EXACT placement at the root — descendant-matching there would let
any top-level import excuse a mis-pathed package test placed anywhere under
``tests/``.

A tiny reviewed exemption set keeps legitimate cross-cutting tests out of the
count: shared dir prefixes (``tests/integration/``, ``tests/conformance/``,
``tests/e2e*``, ``tests/fixtures/``, ``tests/eval_replay/``,
``tests/eval_harness/``), the non-test scaffolding files (``conftest.py``,
``factories.py``, ``__init__.py``), and a per-file ``# test-path: cross-cutting``
line pragma for genuine multi-package contract/architecture tests.
``tests/eval_replay/`` and ``tests/eval_harness/`` are the behavioral-eval test
suites — deterministic replay of and model-patched exercises over the eval
definitions under the top-level ``evals/`` tree (``evals/scenarios``,
``evals/fixtures``). They are organized by the eval lane they belong to, not
mirrored to the ``teatree.eval`` package, exactly as ``tests/integration/`` and
``tests/conformance/`` organize by purpose rather than by src package.

Verdict (mirrors :class:`teatree.quality.mutation_run.BaselineRatchet`): the
live violation count may only ever shrink. ``live_violations > baseline`` ⇒
regression (exit 1); ``<= baseline`` ⇒ exit 0. ``--update-baseline`` rewrites
the floor to the current count but REFUSES to write a HIGHER number without
``--allow-regression``.

Self-contained (stdlib + ``tomllib`` only): ``teatree.quality`` declares no
internal tach dependency this module needs.
"""

import ast
import dataclasses
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, ClassVar

CROSS_CUTTING_PRAGMA = "test-path: cross-cutting"
_FIRST_PARTY_ROOT = "teatree"
_TEST_DIR_PREFIX = "teatree_"
_PACKAGE_MODULE_PARTS = 2
_TEST_FILE_PREFIXES = ("test_",)
_TEST_FILE_SUFFIXES = ("_test.py",)

EXEMPT_DIR_PREFIXES: frozenset[str] = frozenset(
    {
        "tests/integration/",
        "tests/conformance/",
        "tests/e2e",
        "tests/fixtures/",
        "tests/eval_replay/",
        "tests/eval_harness/",
    }
)
EXEMPT_FILENAMES: frozenset[str] = frozenset({"conftest.py", "factories.py", "__init__.py"})


@dataclasses.dataclass(frozen=True)
class MirrorViolation:
    path: str
    imported_modules: tuple[str, ...]
    expected_dirs: tuple[str, ...]

    @property
    def message(self) -> str:
        if self.expected_dirs:
            joined = ", ".join(self.expected_dirs)
            return (
                f"{self.path}: imports {', '.join(self.imported_modules)} but is not under any "
                f"mirror dir ({joined}). Move it to mirror src/teatree/<pkg>/... as tests/teatree_<pkg>/...,"
                " or mark it cross-cutting with a `# test-path: cross-cutting` pragma."
            )
        return (
            f"{self.path}: no first-party teatree import resolves to a mirror dir. "
            "Move it under tests/teatree_<pkg>/... to mirror the module it tests, "
            "or mark it cross-cutting with a `# test-path: cross-cutting` pragma."
        )


@dataclasses.dataclass(frozen=True)
class MirrorReport:
    __test__: ClassVar[bool] = False

    violations: tuple[MirrorViolation, ...]
    baseline: int

    @property
    def live_count(self) -> int:
        return len(self.violations)

    @property
    def exceeds_baseline(self) -> bool:
        return self.live_count > self.baseline

    def summary_lines(self) -> list[str]:
        return [f"  - {violation.message}" for violation in self.violations]


def _is_test_file(path: Path) -> bool:
    name = path.name
    return name.startswith(_TEST_FILE_PREFIXES) or name.endswith(_TEST_FILE_SUFFIXES)


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def is_exempt(path: Path, root: Path) -> bool:
    if path.name in EXEMPT_FILENAMES:
        return True
    rel = _rel_posix(path, root)
    if any(rel.startswith(prefix) for prefix in EXEMPT_DIR_PREFIXES):
        return True
    return has_cross_cutting_pragma(_read(path))


def has_cross_cutting_pragma(source: str) -> bool:
    return any(CROSS_CUTTING_PRAGMA in line for line in source.splitlines())


def first_party_imports(source: str) -> tuple[str, ...]:
    """Top-level ``teatree.<dotted>`` modules imported by *source* (AST, no execution)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    modules: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names if _is_first_party(alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and _is_first_party(node.module):
            modules.append(node.module)
    return tuple(dict.fromkeys(modules))


def _is_first_party(dotted: str) -> bool:
    return dotted == _FIRST_PARTY_ROOT or dotted.startswith(f"{_FIRST_PARTY_ROOT}.")


_TESTS_ROOT = "tests"


@dataclasses.dataclass(frozen=True)
class ExpectedDir:
    path: str
    exact_only: bool

    def satisfied_by(self, actual_dir: str) -> bool:
        """Does *actual_dir* mirror this expectation?

        A package expectation is satisfied by descendant-or-equal containment (a
        deeper sub-package dir still mirrors the package). A ``tests``-root
        expectation (a test whose subject is a top-level src module) demands EXACT
        equality: the test must sit AT the root. Without ``exact_only`` the root
        expectation would match every dir under ``tests/`` via the ``startswith``
        branch, so importing any top-level module would excuse a mis-pathed
        package test placed anywhere — the loophole this closes.
        """
        if self.exact_only:
            return actual_dir == self.path
        return actual_dir == self.path or actual_dir.startswith(f"{self.path}/")


def expected_test_dir(module: str, root: Path) -> ExpectedDir | None:
    """Map ``teatree.<pkg>.<sub>.<leaf>`` to its expected test dir ``tests/teatree_<pkg>/<sub>``.

    The module's own leaf segment is dropped — a module maps to the directory its
    test lives in, never to a file. ``teatree.<X>`` where ``src/teatree/<X>.py`` is
    a top-level MODULE (not a package) maps to the ``tests`` root as an
    ``exact_only`` expectation (its test must sit AT the root, not anywhere under
    it). ``teatree`` alone resolves to nothing (no package to mirror).
    """
    parts = module.split(".")
    if parts[0] != _FIRST_PARTY_ROOT or len(parts) < _PACKAGE_MODULE_PARTS:
        return None
    if len(parts) == _PACKAGE_MODULE_PARTS and _is_top_level_module(parts[1], root):
        return ExpectedDir(path=_TESTS_ROOT, exact_only=True)
    package = f"{_TEST_DIR_PREFIX}{parts[1]}"
    intermediate = parts[2:-1]
    return ExpectedDir(path="/".join([_TESTS_ROOT, package, *intermediate]), exact_only=False)


def _is_top_level_module(name: str, root: Path) -> bool:
    src = root / "src" / _FIRST_PARTY_ROOT
    return (src / f"{name}.py").is_file() and not (src / name).is_dir()


def _expected_dirs(modules: Iterable[str], root: Path) -> tuple[ExpectedDir, ...]:
    dirs = (expected_test_dir(module, root) for module in modules)
    seen: dict[tuple[str, bool], ExpectedDir] = {}
    for d in dirs:
        if d is not None:
            seen.setdefault((d.path, d.exact_only), d)
    return tuple(seen.values())


def _mirrors(actual_dir: str, expected_dirs: Iterable[ExpectedDir]) -> bool:
    return any(expected.satisfied_by(actual_dir) for expected in expected_dirs)


def check_file(path: Path, root: Path) -> MirrorViolation | None:
    if not _is_test_file(path) or is_exempt(path, root):
        return None
    modules = first_party_imports(_read(path))
    if not modules:
        return None
    expected = _expected_dirs(modules, root)
    actual_dir = _rel_posix(path.parent, root)
    if _mirrors(actual_dir, expected):
        return None
    return MirrorViolation(
        path=_rel_posix(path, root),
        imported_modules=modules,
        expected_dirs=tuple(d.path for d in expected),
    )


def collect_test_files(root: Path) -> list[Path]:
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    return sorted(p for p in tests_dir.rglob("*.py"))


def find_violations(root: Path) -> list[MirrorViolation]:
    violations = (check_file(path, root) for path in collect_test_files(root))
    return [violation for violation in violations if violation is not None]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@dataclasses.dataclass(frozen=True)
class MirrorConfig:
    __test__: ClassVar[bool] = False

    mode: str = "warn"
    baseline: int = 0


def load_config(pyproject: Path) -> MirrorConfig:
    raw = _read_table(pyproject)
    mode = str(raw["mode"]) if "mode" in raw else "warn"
    baseline = int(str(raw["baseline"])) if "baseline" in raw else 0
    return MirrorConfig(mode=mode, baseline=baseline)


def _read_table(pyproject: Path) -> Mapping[str, Any]:
    if not pyproject.is_file():
        return {}
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    tool = data.get("tool", {})
    teatree = tool.get("teatree", {}) if isinstance(tool, dict) else {}
    table = teatree.get("test_path_mirror", {}) if isinstance(teatree, dict) else {}
    return table if isinstance(table, dict) else {}


def build_report(*, root: Path, config: MirrorConfig) -> MirrorReport:
    return MirrorReport(violations=tuple(find_violations(root)), baseline=config.baseline)


def loosens_baseline(*, measured: int, baseline: int) -> bool:
    """True when re-baselining to *measured* would record a HIGHER floor than *baseline*.

    The ratchet only moves down. An update that raises the committed count is a
    silent loosening that makes the gate vacuous (a regression "fixed" by
    re-baselining to the regressed value), so the CLI refuses it unless the rise
    is explicitly authorised. An update at or below the committed count tightens
    (or holds) the ratchet and is always allowed.
    """
    return measured > baseline
