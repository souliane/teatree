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
        ceiling = known.ceiling or lane_ceiling
        if known.states_an_unreadable_ceiling:
            unresolved.add(source_file)
        elif seconds >= TIGHT_FRACTION * ceiling:
            pressured.append(CeilingPressure(node_id=node_id, seconds=seconds, ceiling=ceiling))

    pressured.sort(key=lambda pressure: (-pressure.consumed, pressure.node_id))
    return HeadroomReport(pressured=tuple(pressured), judged=len(recorded), unresolved_ceilings=len(unresolved))


def _lane_ceiling(pyproject: Path) -> float | None:
    try:
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    timeout = config.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("timeout")
    return float(timeout) if isinstance(timeout, int | float) else None


@dataclasses.dataclass(frozen=True)
class _FileFacts:
    """What one source file says about its own tests: which exist, and under which ceiling."""

    ceiling: float | None
    states_an_unreadable_ceiling: bool
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


def _read_file_facts(source_file: Path) -> _FileFacts | None:
    """Parse *source_file*; ``None`` when it is gone or unparsable, so nothing is judged from it.

    A file whose marker names its ceiling rather than writing it (``timeout=BUDGET``)
    is missing evidence, not a lane-ceiling file — judging its tests against the lane
    value would invent a verdict the source does not support.
    """
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None

    stated: list[float] = []
    unreadable = False
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Call) and _dotted_name(node.func) == _TIMEOUT_MARKER:
            seconds = _stated_seconds(node)
            if seconds is None:
                unreadable = True
            else:
                stated.append(seconds)
    return _FileFacts(
        ceiling=max(stated) if stated else None,
        states_an_unreadable_ceiling=unreadable,
        function_names=frozenset(names),
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
