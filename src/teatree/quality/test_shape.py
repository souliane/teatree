"""Conservative test-shape enforcement — baseline-ratchet, report-first.

The repo's testing doctrine (``/ac-python`` § Integration-First + Test
Conciseness): integration tests for happy paths, unit tests for edge cases;
parametrize 3+ near-identical tests; keep the test:source ratio from rotting.
This module nudges toward that doctrine WITHOUT wedging legitimate PRs.

Two conservative, explainable heuristics:

1.  **Near-duplicate test functions** — :data:`DupConfig.min_cluster` (default 3)
    or more test functions in one file whose ASTs hash to the same shape and
    that are not already wrapped by a ``@pytest.mark.parametrize`` /
    ``@parametrize`` decorator. The shape hash normalises away identifier names
    and literal *values* but keeps the structural skeleton, so five methods that
    differ only by an input string collapse to one cluster while genuinely
    distinct tests do not. The must-NOT-FLAG dimension (a parametrized test, a
    justified edge-case unit test, legit distinct tests) is what this normalised
    hash protects: a parametrized test is a single function, and distinct tests
    have distinct skeletons.

2.  **Test:source ratio regression vs a committed baseline** — never an absolute
    magic threshold. :func:`measure_ratio` counts test files' and source files'
    non-blank/non-comment lines; :class:`Baseline` is the committed snapshot
    (``[tool.teatree.test_shape]`` in ``pyproject.toml``). A finding fires only
    when the live ratio drops below the baseline ratio by more than
    :data:`Baseline.tolerance` — i.e. the test:source ratio got measurably
    WORSE. The existing state never fails; only a regression past it does.

Default mode is :attr:`Mode.WARN` — a non-blocking report. :attr:`Mode.BLOCK`
is opt-in via ``[tool.teatree.test_shape] mode = "block"``. This is a CI/report
check, never a PreToolUse gate, so it can never lock the agent's tools.

This module is intentionally self-contained (stdlib + ``tomllib`` only):
``teatree.quality`` declares no internal dependencies in ``tach.toml``.
"""

import ast
import dataclasses
import tomllib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

_PARAMETRIZE_DECORATORS: frozenset[str] = frozenset({"parametrize"})
_TEST_FN_PREFIX = "test_"


class Mode(StrEnum):
    WARN = "warn"
    BLOCK = "block"

    @classmethod
    def parse(cls, value: str) -> "Mode":
        normalised = value.strip().lower()
        try:
            return cls(normalised)
        except ValueError as exc:
            valid = ", ".join(m.value for m in cls)
            msg = f"Invalid test_shape mode {value!r}; valid values: {valid}"
            raise ValueError(msg) from exc


@dataclasses.dataclass(frozen=True)
class DupConfig:
    min_cluster: int = 3


@dataclasses.dataclass(frozen=True)
class Baseline:
    test_lines: int
    source_lines: int
    tolerance: float = 0.01

    @property
    def ratio(self) -> float:
        return self.test_lines / self.source_lines if self.source_lines else 0.0


@dataclasses.dataclass(frozen=True)
class RatioMeasurement:
    test_lines: int
    source_lines: int

    @property
    def ratio(self) -> float:
        return self.test_lines / self.source_lines if self.source_lines else 0.0


@dataclasses.dataclass(frozen=True)
class DuplicateCluster:
    path: str
    functions: tuple[str, ...]

    @property
    def message(self) -> str:
        joined = ", ".join(self.functions)
        return (
            f"{self.path}: {len(self.functions)} near-identical test functions "
            f"({joined}) share one shape and are not parametrized. "
            "Collapse them into a single @pytest.mark.parametrize."
        )


@dataclasses.dataclass(frozen=True)
class ShadowedFixture:
    path: str
    name: str
    ancestor_conftest: str

    @property
    def message(self) -> str:
        return (
            f"{self.path}: autouse fixture {self.name!r} shadows an ancestor "
            f"autouse fixture of the same name in conftest "
            f"{self.ancestor_conftest!r}. The ancestor already applies it to this "
            "file; delete the local copy (confirm the bodies match first)."
        )


@dataclasses.dataclass(frozen=True)
class RatioRegression:
    measured: RatioMeasurement
    baseline: Baseline

    @property
    def message(self) -> str:
        return (
            f"test:source ratio regressed to {self.measured.ratio:.3f} "
            f"(test {self.measured.test_lines} / source {self.measured.source_lines}) "
            f"from baseline {self.baseline.ratio:.3f} "
            f"(tolerance {self.baseline.tolerance:.3f}). "
            "Add tests for the new source, or re-baseline with "
            "`t3 tool test-shape --update-baseline` if the drop is intentional."
        )


@dataclasses.dataclass(frozen=True)
class TestShapeReport:
    __test__: ClassVar[bool] = False

    duplicate_clusters: tuple[DuplicateCluster, ...]
    ratio_regression: RatioRegression | None
    mode: Mode
    shadowed_fixtures: tuple[ShadowedFixture, ...] = ()

    @property
    def has_findings(self) -> bool:
        return bool(self.duplicate_clusters) or self.ratio_regression is not None or bool(self.shadowed_fixtures)

    @property
    def should_block(self) -> bool:
        return self.mode is Mode.BLOCK and self.has_findings

    def summary_lines(self) -> list[str]:
        lines: list[str] = [f"  - {cluster.message}" for cluster in self.duplicate_clusters]
        lines.extend(f"  - {fixture.message}" for fixture in self.shadowed_fixtures)
        if self.ratio_regression is not None:
            lines.append(f"  - {self.ratio_regression.message}")
        return lines


def _shape_tokens(node: ast.AST) -> list[str]:
    """Render an AST subtree as a structural skeleton.

    Identifier names and literal *values* are erased (``Name`` → ``N``,
    constants → their type tag), so two functions that differ only by the
    variables and literals they touch normalise to the same token list. The
    node-type sequence (the control-flow + call skeleton) is preserved, so a
    test with an extra assertion or a different branch does not collapse.
    """
    if isinstance(node, ast.Name):
        return ["N"]
    if isinstance(node, ast.arg):
        return ["arg"]
    if isinstance(node, ast.Constant):
        return [f"C:{type(node.value).__name__}"]
    tokens = [type(node).__name__]
    for child in ast.iter_child_nodes(node):
        tokens.extend(_shape_tokens(child))
    return tokens


def _shape_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    tokens: list[str] = []
    for stmt in node.body:
        tokens.extend(_shape_tokens(stmt))
    return "|".join(tokens)


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _is_parametrized(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return bool(_decorator_names(node) & _PARAMETRIZE_DECORATORS)


def _test_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(_TEST_FN_PREFIX)
    ]


def find_duplicate_clusters(source: str, path: str, config: DupConfig | None = None) -> list[DuplicateCluster]:
    cfg = config or DupConfig()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    by_shape: dict[str, list[str]] = defaultdict(list)
    for fn in _test_functions(tree):
        if not _is_parametrized(fn):
            by_shape[_shape_of(fn)].append(fn.name)

    return [
        DuplicateCluster(path=path, functions=tuple(names))
        for names in by_shape.values()
        if len(names) >= cfg.min_cluster
    ]


def _is_autouse_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        target = dec.func
        is_fixture = (isinstance(target, ast.Attribute) and target.attr == "fixture") or (
            isinstance(target, ast.Name) and target.id == "fixture"
        )
        if not is_fixture:
            continue
        for kw in dec.keywords:
            if kw.arg == "autouse" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
    return False


def autouse_fixture_names(source: str) -> set[str]:
    """Return the names of every ``@pytest.fixture(autouse=True)`` in *source*."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_autouse_fixture(node)
    }


def _ancestor_conftests(test_file: Path, root: Path) -> list[Path]:
    """Conftest.py files on the path from *root* down to *test_file*'s directory.

    A deeper conftest is itself a file that can shadow a shallower one, so the
    list excludes ``test_file`` only — when ``test_file`` is a conftest, its own
    strictly-shallower ancestors are returned.
    """
    conftests: list[Path] = []
    current = test_file.parent
    stack: list[Path] = []
    while True:
        stack.append(current)
        if current in {root, current.parent}:
            break
        current = current.parent
    for directory in reversed(stack):
        candidate = directory / "conftest.py"
        if candidate.is_file() and candidate != test_file:
            conftests.append(candidate)
    return conftests


def find_shadowed_autouse_fixtures(*, test_files: Iterable[Path], root: Path) -> list[ShadowedFixture]:
    """Find autouse fixtures redundantly redefined where an ancestor conftest already provides them.

    A ``@pytest.fixture(autouse=True)`` in a test module or deeper conftest is
    redundant when an ancestor ``conftest.py`` defines an autouse fixture of the
    same name — pytest already applies the ancestor's fixture to every test in
    the subtree, so the local copy is dead duplication.
    """
    conftest_autouse: dict[Path, set[str]] = {}

    def names_for(path: Path) -> set[str]:
        if path not in conftest_autouse:
            conftest_autouse[path] = autouse_fixture_names(_read(path))
        return conftest_autouse[path]

    findings: list[ShadowedFixture] = []
    for test_file in test_files:
        local = autouse_fixture_names(_read(test_file))
        if not local:
            continue
        for ancestor in _ancestor_conftests(test_file, root):
            shadowed = local & names_for(ancestor)
            findings.extend(
                ShadowedFixture(path=str(test_file), name=name, ancestor_conftest=str(ancestor))
                for name in sorted(shadowed)
            )
    return findings


def _significant_line_count(text: str) -> int:
    return sum(1 for raw in text.splitlines() if (s := raw.strip()) and not s.startswith("#"))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def measure_ratio(*, test_files: Iterable[Path], source_files: Iterable[Path]) -> RatioMeasurement:
    test_lines = sum(_significant_line_count(_read(p)) for p in test_files)
    source_lines = sum(_significant_line_count(_read(p)) for p in source_files)
    return RatioMeasurement(test_lines=test_lines, source_lines=source_lines)


def detect_ratio_regression(measured: RatioMeasurement, baseline: Baseline) -> RatioRegression | None:
    if baseline.source_lines <= 0:
        return None
    if measured.ratio < baseline.ratio - baseline.tolerance:
        return RatioRegression(measured=measured, baseline=baseline)
    return None


def loosens_baseline(measured: RatioMeasurement, baseline: Baseline) -> bool:
    """True when rebasing to ``measured`` would write a WORSE ratio than ``baseline``.

    The ratchet is only allowed to move in the improving direction. An update
    that lowers the committed ratio is a silent loosening that makes the check
    vacuous (a regression "fixed" by re-baselining to the regressed value), so
    the CLI refuses it unless the drop is explicitly authorised. An update at or
    above the committed ratio tightens (or holds) the ratchet and is always
    allowed. The first-ever baseline (no committed source lines) loosens nothing.
    """
    if baseline.source_lines <= 0:
        return False
    return measured.ratio < baseline.ratio


@dataclasses.dataclass(frozen=True)
class TestShapeConfig:
    __test__: ClassVar[bool] = False

    mode: Mode = Mode.WARN
    baseline: Baseline | None = None
    dup: DupConfig = dataclasses.field(default_factory=DupConfig)


def load_config(pyproject: Path) -> TestShapeConfig:
    raw = _read_test_shape_table(pyproject)
    mode = Mode.parse(str(raw["mode"])) if "mode" in raw else Mode.WARN
    dup = DupConfig(min_cluster=int(str(raw["min_cluster"]))) if "min_cluster" in raw else DupConfig()
    return TestShapeConfig(mode=mode, baseline=_parse_baseline(raw), dup=dup)


def _read_test_shape_table(pyproject: Path) -> Mapping[str, Any]:
    if not pyproject.is_file():
        return {}
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    tool = data.get("tool", {})
    teatree = tool.get("teatree", {}) if isinstance(tool, dict) else {}
    table = teatree.get("test_shape", {}) if isinstance(teatree, dict) else {}
    return table if isinstance(table, dict) else {}


def _parse_baseline(raw: Mapping[str, Any]) -> Baseline | None:
    if "test_lines" not in raw or "source_lines" not in raw:
        return None
    tolerance = float(str(raw["tolerance"])) if "tolerance" in raw else 0.01
    return Baseline(
        test_lines=int(str(raw["test_lines"])),
        source_lines=int(str(raw["source_lines"])),
        tolerance=tolerance,
    )


def collect_test_files(root: Path) -> list[Path]:
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    return sorted(p for p in tests_dir.rglob("*.py") if p.name != "__init__.py")


def collect_source_files(root: Path) -> list[Path]:
    src_dir = root / "src" / "teatree"
    if not src_dir.is_dir():
        return []
    return sorted(p for p in src_dir.rglob("*.py") if "migrations" not in p.parts)


def build_report(
    *,
    test_files: Sequence[Path],
    source_files: Sequence[Path],
    config: TestShapeConfig,
    root: Path | None = None,
) -> TestShapeReport:
    clusters: list[DuplicateCluster] = []
    for path in test_files:
        clusters.extend(find_duplicate_clusters(_read(path), str(path), config.dup))

    ratio_regression: RatioRegression | None = None
    if config.baseline is not None:
        measured = measure_ratio(test_files=test_files, source_files=source_files)
        ratio_regression = detect_ratio_regression(measured, config.baseline)

    shadowed: tuple[ShadowedFixture, ...] = ()
    if root is not None:
        shadowed = tuple(find_shadowed_autouse_fixtures(test_files=test_files, root=root))

    return TestShapeReport(
        duplicate_clusters=tuple(clusters),
        ratio_regression=ratio_regression,
        mode=config.mode,
        shadowed_fixtures=shadowed,
    )
