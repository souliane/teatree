"""Judge-rubric classification: every judge-bearing scenario's criteria, typed.

`evals/judge_rubric_classification.yaml` carries one entry per judge-bearing
``EvalSpec.name`` (issue #4819's "classify the judge rubrics' criteria into
narrow, holistic and assertion" step). Each criterion is classified and
carries a ``reason``; an ``assertion`` criterion additionally records whether
it has been migrated to a deterministic matcher (and if not, WHY it is
deferred rather than a silent gap). A future judge rubric added with no
classification entry is a GAP, closing the silent-regrowth hole the issue
names — mirrors :mod:`teatree.eval.coverage`'s shape (a pure function over
:func:`~teatree.eval.discovery.discover_specs` plus a side-table), feeding the
``test_judge_rubric_classification_coverage`` pytest gate.
"""

import dataclasses
from pathlib import Path
from typing import Any, Literal

import yaml

from teatree.eval.discovery import discover_specs
from teatree.eval.models import EvalSpec

#: ``skills/`` sits next to ``src/`` in the teatree tree; resolve `evals/` the
#: same way `discovery.py` resolves `SCENARIOS_DIR`, so this stays a leaf module.
CLASSIFICATION_PATH = Path(__file__).resolve().parents[3] / "evals" / "judge_rubric_classification.yaml"

CriterionClass = Literal["narrow", "holistic", "assertion"]
_VALID_CLASSES = frozenset({"narrow", "holistic", "assertion"})


class JudgeRubricClassificationError(ValueError):
    """The classification YAML is missing, unreadable, or fails its own schema."""


@dataclasses.dataclass(frozen=True)
class RubricCriterion:
    criterion: str
    criterion_class: CriterionClass
    reason: str
    migrated: bool | None = None
    matcher: str | None = None


@dataclasses.dataclass(frozen=True)
class SpecClassification:
    spec_name: str
    criteria: tuple[RubricCriterion, ...]


@dataclasses.dataclass(frozen=True)
class ClassificationReport:
    rows: tuple[SpecClassification, ...]
    #: Judge-bearing spec names with no classification entry at all.
    gaps: tuple[str, ...]
    #: Classification entries naming a spec that is no longer judge-bearing
    #: (renamed, deleted, or its judge block was fully dropped).
    stale: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.gaps


def _load_classification(path: Path) -> dict[str, Any]:
    """Raw, not-yet-validated YAML content — untyped third-party data by nature.

    Each entry's shape is checked field-by-field in :func:`_parse_entry` /
    :func:`_parse_criterion`, which is where the real typing lives.
    """
    if not path.is_file():
        msg = f"judge-rubric classification file is missing: {path}"
        raise JudgeRubricClassificationError(msg)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        msg = f"{path} must parse to a mapping of spec name -> classification, got {type(data).__name__}"
        raise JudgeRubricClassificationError(msg)
    return data


def _require_str(spec_name: str, index: int, criterion: str | None, field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        label = f"{spec_name} criteria[{index}]" + (f" ({criterion!r})" if criterion else "")
        msg = f"{label} is missing a non-empty {field!r}"
        raise JudgeRubricClassificationError(msg)
    return value.strip()


def _parse_criterion(spec_name: str, index: int, raw: object) -> RubricCriterion:
    if not isinstance(raw, dict):
        msg = f"{spec_name} criteria[{index}] must be a mapping, got {type(raw).__name__}"
        raise JudgeRubricClassificationError(msg)
    criterion = _require_str(spec_name, index, None, "criterion", raw.get("criterion"))
    criterion_class = raw.get("class")
    if criterion_class not in _VALID_CLASSES:
        msg = (
            f"{spec_name} criteria[{index}] ({criterion!r}) has class {criterion_class!r}, "
            f"must be one of {sorted(_VALID_CLASSES)}"
        )
        raise JudgeRubricClassificationError(msg)
    reason = _require_str(spec_name, index, criterion, "reason", raw.get("reason"))
    migrated = raw.get("migrated")
    matcher = raw.get("matcher")
    if criterion_class == "assertion":
        if not isinstance(migrated, bool):
            msg = (
                f"{spec_name} criteria[{index}] ({criterion!r}) is class 'assertion' "
                "and must set 'migrated: true|false'"
            )
            raise JudgeRubricClassificationError(msg)
        if migrated:
            matcher = _require_str(spec_name, index, criterion, "matcher", matcher)
        elif matcher is not None:
            msg = f"{spec_name} criteria[{index}] ({criterion!r}) sets 'matcher' but 'migrated' is false"
            raise JudgeRubricClassificationError(msg)
    elif migrated is not None or matcher is not None:
        msg = (
            f"{spec_name} criteria[{index}] ({criterion!r}) is class {criterion_class!r} "
            "and must not set 'migrated'/'matcher'"
        )
        raise JudgeRubricClassificationError(msg)
    return RubricCriterion(
        criterion=criterion,
        criterion_class=criterion_class,
        reason=reason,
        migrated=migrated if criterion_class == "assertion" else None,
        matcher=matcher if isinstance(matcher, str) and matcher.strip() else None,
    )


def _parse_entry(spec_name: str, raw: object) -> SpecClassification:
    if not isinstance(raw, dict) or not isinstance(raw.get("criteria"), list) or not raw["criteria"]:
        msg = f"{spec_name} must map to {{'criteria': [...]}} with at least one criterion"
        raise JudgeRubricClassificationError(msg)
    criteria = tuple(_parse_criterion(spec_name, i, item) for i, item in enumerate(raw["criteria"]))
    return SpecClassification(spec_name=spec_name, criteria=criteria)


def judge_rubric_coverage(
    specs: list[EvalSpec] | None = None,
    classification_path: Path = CLASSIFICATION_PATH,
) -> ClassificationReport:
    """Classify every judge-bearing spec's rubric criteria, or report the gap."""
    if specs is None:
        specs = discover_specs()
    judge_bearing = sorted({spec.name for spec in specs if spec.judge is not None})
    entries = {name: _parse_entry(name, raw) for name, raw in _load_classification(classification_path).items()}
    gaps = tuple(name for name in judge_bearing if name not in entries)
    stale = tuple(name for name in entries if name not in judge_bearing)
    rows = tuple(entries[name] for name in sorted(entries) if name in judge_bearing)
    return ClassificationReport(rows=rows, gaps=gaps, stale=stale)
