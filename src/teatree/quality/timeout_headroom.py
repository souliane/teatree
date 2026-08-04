"""How much room each recorded test has left against its own ceiling (#4048).

The sharded CI lane raises the per-test ceiling because twelve shards contending
on one runner turn a test's real cost into something the tight local ceiling was
never sized for. A raise alone would only move the cliff: a test drifting toward
the new ceiling is invisible until the day it reds a PR whose diff cannot have
caused it, which is the failure this epic exists to remove. So the raise ships
with a record of what it bought — per test, seconds recorded against the ceiling
that actually applies to it.

Which ceiling applies is decided the way ``pytest_timeout._get_item_settings``
decides it: a ``@pytest.mark.timeout`` on (or above) the test wins, and the lane
value applies to everything else. A marker only ever raises, so a test below the
tight band at the lane ceiling cannot be pressured under any marker — that is
what keeps the source scan down to the handful of files a candidate names.
"""

import ast
import dataclasses
import tomllib
from pathlib import Path

from teatree.quality.durations_file import DURATIONS_PATH, read_durations

# Under this much room left, a test is one contended run from timing out — the
# state worth naming while it is still a warning rather than a red check.
TIGHT_FRACTION = 0.75

_TIMEOUT_MARKER = "pytest.mark.timeout"


@dataclasses.dataclass(frozen=True)
class CeilingPressure:
    node_id: str
    seconds: float
    ceiling: float

    @property
    def consumed(self) -> float:
        return self.seconds / self.ceiling

    @property
    def is_over(self) -> bool:
        return self.seconds > self.ceiling


@dataclasses.dataclass(frozen=True)
class HeadroomReport:
    pressured: tuple[CeilingPressure, ...]
    judged: int
    unresolved_ceilings: int

    @property
    def over_ceiling(self) -> tuple[CeilingPressure, ...]:
        return tuple(pressure for pressure in self.pressured if pressure.is_over)

    @property
    def is_healthy(self) -> bool:
        return not self.over_ceiling


def measure_timeout_headroom(repo: Path) -> HeadroomReport | None:
    """Return *repo*'s recorded ceiling pressure, or ``None`` if this venue cannot judge it.

    ``None`` is "no lane ceiling to judge against" (an installed teatree with no
    checkout, or a pyproject that sets none), never "healthy" — the caller stays
    silent rather than reporting a verdict it did not establish.
    """
    lane_ceiling = _lane_ceiling(repo / "pyproject.toml")
    if lane_ceiling is None:
        return None

    recorded = read_durations(repo / DURATIONS_PATH)
    candidates = {node_id: seconds for node_id, seconds in recorded.items() if seconds >= TIGHT_FRACTION * lane_ceiling}

    pressured: list[CeilingPressure] = []
    unresolved: set[str] = set()
    facts: dict[str, _FileFacts | None] = {}
    for node_id, seconds in candidates.items():
        source_file = node_id.split("::", 1)[0]
        if source_file not in facts:
            facts[source_file] = _read_file_facts(repo / source_file)
        known = facts[source_file]
        if known is None or not known.defines(node_id):
            continue
        stated = known.ceiling_for(node_id)
        if stated.is_unreadable:
            unresolved.add(source_file)
            continue
        ceiling = stated.seconds or lane_ceiling
        if seconds >= TIGHT_FRACTION * ceiling:
            pressured.append(CeilingPressure(node_id=node_id, seconds=seconds, ceiling=ceiling))

    pressured.sort(key=lambda pressure: (-pressure.consumed, pressure.node_id))
    return HeadroomReport(pressured=tuple(pressured), judged=len(recorded), unresolved_ceilings=len(unresolved))


def _lane_ceiling(pyproject: Path) -> float | None:
    try:
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a doctor check must never crash the run
        return None
    timeout = config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("timeout")
    return float(timeout) if isinstance(timeout, int | float) else None


@dataclasses.dataclass(frozen=True)
class _StatedCeiling:
    """What the source says the ceiling is for one test: a number, silence, or an unreadable claim."""

    seconds: float | None = None
    is_unreadable: bool = False


_LANE = _StatedCeiling()


@dataclasses.dataclass(frozen=True)
class _FileFacts:
    """What one source file says about its own tests: which exist, and under which ceiling."""

    module_ceiling: _StatedCeiling
    ceilings: dict[str, _StatedCeiling]
    function_names: frozenset[str]

    def defines(self, node_id: str) -> bool:
        """Whether *node_id*'s test still exists here.

        A durations file outlives the tests it recorded: a renamed or split test
        leaves a key whose FILE is still present, so a file-level existence check
        passes and the stale recording gets judged as if it were a live test. That
        is the same class of red this epic removes, one level in. A doctest key
        names an object rather than a test function, so it is taken at face value.
        """
        leaf = node_id.rsplit("::", 1)[-1].split("[", 1)[0]
        return "." in leaf or leaf in self.function_names

    def ceiling_for(self, node_id: str) -> _StatedCeiling:
        """The ceiling that applies to *node_id* alone, never a neighbour's.

        A key whose in-file path is not one of the collected definitions (a doctest
        object, a test reached through a shape the scan does not model) falls back
        to what the module states, which is what pytest applies absent a closer marker.
        """
        path = node_id.split("::")[1:]
        if path:
            path[-1] = path[-1].split("[", 1)[0]
        return self.ceilings.get("::".join(path), self.module_ceiling)


class _MarkerScan:
    """Resolves each test's ceiling the way ``pytest`` picks an item's closest marker.

    A marker on the function beats one on its class, which beats one on the module —
    so the ceiling is attributed to the definition it decorates and to nothing else.
    A file-wide reading lets one slow test's raise cover every neighbour, and an
    unmarked test over-running the lane value then reads as healthy.

    Ties within one definition go to the mark pytest STORES first, which is not the
    one read first in source order: decorators apply bottom-up, and a class body runs
    before the decorators applied to it. So the bottom-most decorator wins, and a
    class-body ``pytestmark`` beats a decorator on that same class. Reading source
    order instead over-states the ceiling — the silent-green direction.

    A marker this scan cannot see (one inherited through a base class's MRO, or added
    by a hook) leaves the test on the module or lane value, which UNDER-states its
    ceiling and can only over-report. That is the direction a health check may err in.
    """

    def __init__(self, module: ast.Module) -> None:
        self._aliases = self._aliases_in(module.body)
        self.module_ceiling = self._pytestmark_in(module.body) or _LANE
        self.ceilings: dict[str, _StatedCeiling] = {}
        self._collect(module.body, self.module_ceiling, ())

    def _collect(self, body: list[ast.stmt], inherited: _StatedCeiling, prefix: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                own = self._pytestmark_in(node.body) or self._decorated(node.decorator_list)
                self._collect(node.body, own or inherited, (*prefix, node.name))
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.ceilings["::".join((*prefix, node.name))] = self._decorated(node.decorator_list) or inherited

    def _decorated(self, decorators: list[ast.expr]) -> _StatedCeiling | None:
        return self._first_marker(list(reversed(decorators)))

    def _aliases_in(self, body: list[ast.stmt]) -> dict[str, _StatedCeiling]:
        """Module names bound to a marker (``_SCAN_TIMEOUT = pytest.mark.timeout(300)``), reused as decorators."""
        aliases: dict[str, _StatedCeiling] = {}
        for node in body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            stated = self._marker(node.value)
            if stated is not None:
                aliases.update({target.id: stated for target in node.targets if isinstance(target, ast.Name)})
        return aliases

    def _pytestmark_in(self, body: list[ast.stmt]) -> _StatedCeiling | None:
        for node in body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets):
                continue
            elements = node.value.elts if isinstance(node.value, ast.List | ast.Tuple) else [node.value]
            stated = self._first_marker(elements)
            if stated is not None:
                return stated
        return None

    def _first_marker(self, expressions: list[ast.expr]) -> _StatedCeiling | None:
        for expression in expressions:
            if isinstance(expression, ast.Call):
                stated = self._marker(expression)
                if stated is not None:
                    return stated
            elif isinstance(expression, ast.Name) and expression.id in self._aliases:
                return self._aliases[expression.id]
        return None

    @staticmethod
    def _marker(call: ast.Call) -> _StatedCeiling | None:
        if _dotted_name(call.func) != _TIMEOUT_MARKER:
            return None
        seconds = _stated_seconds(call)
        return _StatedCeiling(seconds=seconds, is_unreadable=seconds is None)


def _read_file_facts(source_file: Path) -> _FileFacts | None:
    """Parse *source_file*; ``None`` when it is gone or unparsable, so nothing is judged from it.

    A marker that names its ceiling rather than writing it (``timeout=BUDGET``) is
    missing evidence for the tests IT covers — judging those against the lane value
    would invent a verdict the source does not support — while every other test in
    the file keeps the ceiling that really applies to it.
    """
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a doctor check must never crash the run
        return None

    scan = _MarkerScan(tree)
    return _FileFacts(
        module_ceiling=scan.module_ceiling,
        ceilings=scan.ceilings,
        function_names=frozenset(
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ),
    )


def _dotted_name(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _stated_seconds(call: ast.Call) -> float | None:
    argument = next((keyword.value for keyword in call.keywords if keyword.arg == "timeout"), None)
    if argument is None and call.args:
        argument = call.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, int | float):
        return float(argument.value)
    return None
