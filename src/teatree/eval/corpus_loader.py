"""Load ground-truth corpus labels from ``corpus/*.label.yaml`` into typed dataclasses.

Mirrors :mod:`teatree.eval.loader`: each ``<entry_id>.label.yaml`` is validated
at load time and raises :class:`~teatree.eval.loader.EvalSpecError` with the
offending file path. The matcher and judge sub-schemas are the same as an eval
spec's, so their parsers are reused verbatim rather than re-implemented.

A label references a sibling ``<entry_id>.session.jsonl`` capture; the loader
refuses a label whose session file is missing, an ``oracle: matcher`` label with
no matchers, an ``oracle: judge`` label with no ``judge.rubric``, and a corpus
directory carrying two labels for the same ``entry_id``.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, get_args

import yaml

from teatree.eval.corpus_models import Confidence, CorpusLabel, Oracle
from teatree.eval.loader import EvalSpecError, _parse_judge, _parse_matcher

CORPUS_DIR = Path(__file__).parent / "corpus"

_CONFIDENCES: frozenset[str] = frozenset(get_args(Confidence))
_ORACLES: frozenset[str] = frozenset(get_args(Oracle))


def discover_corpus(directory: Path | None = None) -> list[CorpusLabel]:
    """Load every ``*.label.yaml`` under *directory* (defaults to the shipped corpus).

    Sorted by ``entry_id``; a directory carrying two labels for one ``entry_id``
    is rejected so a duplicate cannot silently shadow a ground-truth entry.
    """
    root = CORPUS_DIR if directory is None else directory
    labels = [load_corpus_label(path) for path in sorted(root.glob("*.label.yaml"))]
    seen: set[str] = set()
    for label in labels:
        if label.entry_id in seen:
            raise EvalSpecError(root, None, f"duplicate entry_id {label.entry_id!r} in corpus")
        seen.add(label.entry_id)
    return sorted(labels, key=lambda label: label.entry_id)


def load_corpus_label(path: Path) -> CorpusLabel:
    text = path.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line = getattr(getattr(exc, "problem_mark", None), "line", None)
        raise EvalSpecError(path, (line + 1) if line is not None else None, str(exc)) from exc
    if not isinstance(loaded, list) or len(loaded) != 1 or not isinstance(loaded[0], Mapping):
        raise EvalSpecError(path, None, "expected a single-entry YAML list with one label mapping")
    return _parse_label({str(k): v for k, v in loaded[0].items()}, path)


def _parse_label(entry: Mapping[str, Any], path: Path) -> CorpusLabel:
    entry_id = _required_str(entry, "entry_id", path)
    session_path = path.with_name(f"{entry_id}.session.jsonl")
    if not session_path.is_file():
        raise EvalSpecError(path, None, f"missing session jsonl for entry {entry_id!r}: {session_path.name}")
    oracle = _parse_choice(entry, "oracle", _ORACLES, path)
    confidence = _parse_choice(entry, "confidence", _CONFIDENCES, path)
    matchers = tuple(_parse_matcher(item, entry_id, path) for item in entry.get("expect") or [])
    judge = _parse_judge(entry, entry_id, path)
    _validate_oracle(oracle, matchers=bool(matchers), judge=judge is not None, entry_id=entry_id, path=path)
    return CorpusLabel(
        entry_id=entry_id,
        labelled_by=_required_str(entry, "labelled_by", path),
        labelled_at=_required_str(entry, "labelled_at", path),
        expected_behavior=_required_str(entry, "expected_behavior", path),
        outcome_axis=_required_str(entry, "outcome_axis", path),
        expected_outcome=_required_str(entry, "expected_outcome", path),
        confidence=confidence,  # type: ignore[arg-type]
        oracle=oracle,  # type: ignore[arg-type]
        matchers=matchers,
        judge=judge,
        rule_author=str(entry.get("rule_author") or ""),
        source_session_id=str(entry.get("source_session_id") or ""),
    )


def _validate_oracle(oracle: str, *, matchers: bool, judge: bool, entry_id: str, path: Path) -> None:
    if oracle in {"matcher", "both"} and not matchers:
        raise EvalSpecError(path, None, f"entry {entry_id!r}: oracle {oracle!r} requires a non-empty `expect`")
    if oracle in {"judge", "both"} and not judge:
        raise EvalSpecError(path, None, f"entry {entry_id!r}: oracle {oracle!r} requires a `judge.rubric`")


def _parse_choice(entry: Mapping[str, Any], key: str, allowed: frozenset[str], path: Path) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise EvalSpecError(path, None, f"{key!r} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _required_str(entry: Mapping[str, Any], key: str, path: Path) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalSpecError(path, None, f"required string field missing or empty: {key!r}")
    return value
