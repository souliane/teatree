"""Parse a ``t3 eval run --models ... --format json`` / ``t3 eval benchmark --format json`` payload.

:func:`~teatree.eval.matrix.render_matrix_json` is the writer; this is its
reader counterpart, consumed by ``t3 eval set-baseline``. Kept as pure
structural validation (no CLI, no runner) so a malformed matrix file is a
fail-loud :class:`MatrixPayloadError`, not a silent ``KeyError`` deep inside
the baseline derivation.
"""

import dataclasses
import json
from pathlib import Path


class MatrixPayloadError(RuntimeError):
    """The JSON at *path* is not the ``{models, scenarios}`` shape ``render_matrix_json`` emits."""


@dataclasses.dataclass(frozen=True)
class MatrixCell:
    """One ``(scenario, model)`` verdict, mirroring the cell ``render_matrix_json`` emits."""

    passed: bool
    skipped: bool
    errored: bool
    score: float = 0.0
    trials: int = 1


@dataclasses.dataclass(frozen=True)
class MatrixScenarioEntry:
    """One scenario's per-model result cells, keyed by model id (``None`` = no cell)."""

    name: str
    results: dict[str, MatrixCell | None]


@dataclasses.dataclass(frozen=True)
class MatrixPayload:
    """The parsed matrix: the compared model list plus every scenario's result row."""

    models: list[str]
    scenarios: list[MatrixScenarioEntry]


def load_matrix_payload(path: Path) -> MatrixPayload:
    """Parse and structurally validate *path* as a matrix JSON payload."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"{path}: not valid JSON: {exc}"
        raise MatrixPayloadError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"{path}: top-level JSON must be an object"
        raise MatrixPayloadError(msg)
    models = raw.get("models")
    if not isinstance(models, list) or not all(isinstance(model, str) for model in models):
        msg = f"{path}: 'models' must be a list of strings"
        raise MatrixPayloadError(msg)
    scenarios_raw = raw.get("scenarios")
    if not isinstance(scenarios_raw, list):
        msg = f"{path}: 'scenarios' must be a list"
        raise MatrixPayloadError(msg)
    return MatrixPayload(models=models, scenarios=[_parse_scenario_entry(path, entry) for entry in scenarios_raw])


def _parse_scenario_entry(path: Path, entry: object) -> MatrixScenarioEntry:
    if not isinstance(entry, dict):
        msg = f"{path}: each scenario entry must be an object"
        raise MatrixPayloadError(msg)
    name = entry.get("name")
    if not isinstance(name, str):
        msg = f"{path}: scenario entry missing a string 'name'"
        raise MatrixPayloadError(msg)
    results = entry.get("results")
    if not isinstance(results, dict):
        msg = f"{path}: scenario {name!r} is missing a 'results' object"
        raise MatrixPayloadError(msg)
    return MatrixScenarioEntry(
        name=name,
        results={model: _parse_cell(path, name, model, cell) for model, cell in results.items()},
    )


def _parse_cell(path: Path, scenario: str, model: str, cell: object) -> MatrixCell | None:
    if cell is None:
        return None
    where = f"{path}: scenario {scenario!r}, model {model!r}"
    if not isinstance(cell, dict):
        msg = f"{where}: each result cell must be an object or null"
        raise MatrixPayloadError(msg)
    return MatrixCell(
        passed=_parse_bool(where, "passed", cell.get("passed")),
        skipped=_parse_bool(where, "skipped", cell.get("skipped")),
        errored=_parse_bool(where, "errored", cell.get("errored")),
        score=_parse_score(where, cell.get("score", 0.0)),
        trials=_parse_trials(where, cell.get("trials", 1)),
    )


def _parse_bool(where: str, field: str, value: object) -> bool:
    if not isinstance(value, bool):
        msg = f"{where}: cell {field!r} must be a boolean"
        raise MatrixPayloadError(msg)
    return value


def _parse_score(where: str, value: object) -> float:
    # bool is an int subclass — reject it explicitly so `true` is not read as 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{where}: cell 'score' must be a number"
        raise MatrixPayloadError(msg)
    return float(value)


def _parse_trials(where: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{where}: cell 'trials' must be an integer"
        raise MatrixPayloadError(msg)
    return value
