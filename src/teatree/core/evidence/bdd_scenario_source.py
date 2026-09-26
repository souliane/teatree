"""FINAL BDD source declaration shared by test-plan writers and checkers."""

import json
import re
from collections.abc import Mapping
from typing import Literal, TypedDict

_FIELD = re.compile(
    r"^\s*(?:[-*]\s+)?\*\*(?:PRD page|BDD revision|BDD status|Final scenario IDs):\*\*",
    re.IGNORECASE | re.MULTILINE,
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SOURCE = re.compile(r"<!--\s*t3-bdd-source\b(?P<body>.*?)-->", re.DOTALL)
_TRACE = re.compile(r"<!--\s*t3-bdd-trace\b(?P<body>.*?)-->", re.DOTALL)
_LEGACY_TRACE = re.compile(r"<!--\s*covers?\s+(?P<body>.*?)\s*-->", re.IGNORECASE | re.DOTALL)
BDD_ID = re.compile(r"\bBDD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\b", re.IGNORECASE)


class BddScenarioSource(TypedDict):
    prd_page: str
    bdd_revision: str
    status: str
    scenario_ids: list[str]


class BddSourceError(ValueError):
    def __init__(self, kind: Literal["bdd-source", "bdd-trace"], message: str) -> None:
        self.kind = kind
        super().__init__(message)

    @classmethod
    def missing_fields(cls, missing: list[str]) -> "BddSourceError":
        return cls("bdd-source", "scenario source is missing " + ", ".join(missing) + ".")

    @classmethod
    def not_final(cls, status: str) -> "BddSourceError":
        shown = status or "missing"
        return cls(
            "bdd-source",
            f"BDD status is {shown!r}; customer E2E work requires the ticket's FINAL BDD scenario set.",
        )

    @classmethod
    def duplicate_ids(cls) -> "BddSourceError":
        return cls("bdd-source", "scenario source contains duplicate final scenario IDs.")

    @classmethod
    def missing_declaration(cls) -> "BddSourceError":
        return cls(
            "bdd-source",
            "scenario source is missing; add one hidden `<!-- t3-bdd-source {...} -->` comment "
            "(manifest: `scenario_source`) carrying prd_page, bdd_revision, status `final` and scenario_ids.",
        )

    @classmethod
    def malformed_declaration(cls) -> "BddSourceError":
        return cls("bdd-source", "test plan must carry exactly one hidden `<!-- t3-bdd-source {...} -->` comment.")

    @classmethod
    def invalid_source_json(cls, error: json.JSONDecodeError) -> "BddSourceError":
        return cls("bdd-source", f"the `t3-bdd-source` declaration is not valid JSON: {error}.")

    @classmethod
    def invalid_source_shape(cls) -> "BddSourceError":
        return cls(
            "bdd-source",
            "the `t3-bdd-source` declaration must be a JSON object with prd_page, bdd_revision, status "
            "and scenario_ids.",
        )

    @classmethod
    def visible_declaration(cls) -> "BddSourceError":
        return cls(
            "bdd-source",
            "a scenario-source field (PRD page, BDD revision, BDD status, Final scenario IDs) is visible to the "
            "reader; the source lives only in the hidden `<!-- t3-bdd-source {...} -->` comment.",
        )

    @classmethod
    def invalid_trace_json(cls, error: json.JSONDecodeError) -> "BddSourceError":
        return cls("bdd-trace", f"BDD trace is not valid JSON: {error}.")

    @classmethod
    def invalid_trace_shape(cls) -> "BddSourceError":
        return cls("bdd-trace", "BDD trace must be a JSON array of scenario IDs.")

    @classmethod
    def unknown_trace_ids(cls, unknown: list[str]) -> "BddSourceError":
        return cls(
            "bdd-trace",
            "test plan traces to scenario IDs outside the declared FINAL BDD set: " + ", ".join(unknown) + ".",
        )


def _validated_source(raw: Mapping[str, object]) -> BddScenarioSource:
    prd_page = str(raw.get("prd_page") or "").strip()
    revision = str(raw.get("bdd_revision") or "").strip()
    status = str(raw.get("status") or "").strip().lower()
    raw_ids = raw.get("scenario_ids")
    scenario_ids = [str(item).strip().strip("`") for item in raw_ids] if isinstance(raw_ids, list) else []
    scenario_ids = [item for item in scenario_ids if item]
    missing = [
        label
        for label, value in (
            ("PRD page", prd_page),
            ("BDD revision/date", revision),
            ("final scenario IDs", scenario_ids),
        )
        if not value
    ]
    if missing:
        raise BddSourceError.missing_fields(missing)
    if status != "final":
        raise BddSourceError.not_final(status)
    if len(scenario_ids) != len(set(scenario_ids)):
        raise BddSourceError.duplicate_ids()
    return {
        "prd_page": prd_page,
        "bdd_revision": revision,
        "status": status,
        "scenario_ids": scenario_ids,
    }


def parse_bdd_source_mapping(raw: object) -> BddScenarioSource:
    if not isinstance(raw, dict):
        raise BddSourceError.missing_declaration()
    return _validated_source({str(key): value for key, value in raw.items()})


def _declared_source(body: str) -> BddScenarioSource:
    declarations = [match.group("body") for match in _SOURCE.finditer(body)]
    if not declarations:
        raise BddSourceError.missing_declaration()
    if len(declarations) > 1:
        raise BddSourceError.malformed_declaration()
    try:
        raw = json.loads(declarations[0])
    except json.JSONDecodeError as error:
        raise BddSourceError.invalid_source_json(error) from None
    if not isinstance(raw, dict):
        raise BddSourceError.invalid_source_shape()
    return parse_bdd_source_mapping(raw)


def _traced_scenario_ids(body: str) -> set[str]:
    traced: set[str] = set()
    for match in _TRACE.finditer(body):
        try:
            raw = json.loads(match.group("body"))
        except json.JSONDecodeError as error:
            raise BddSourceError.invalid_trace_json(error) from None
        if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
            raise BddSourceError.invalid_trace_shape()
        traced.update(item.strip() for item in raw)
    for match in _LEGACY_TRACE.finditer(body):
        traced.update(found.group(0) for found in BDD_ID.finditer(match.group("body")))
    return traced


def refuse_visible_bdd_source(body: str) -> None:
    if _FIELD.search(_HTML_COMMENT.sub("", body)):
        raise BddSourceError.visible_declaration()


def validate_bdd_source(body: str) -> BddScenarioSource:
    refuse_visible_bdd_source(body)
    source = _declared_source(body)
    unknown = sorted(_traced_scenario_ids(body) - set(source["scenario_ids"]))
    if unknown:
        raise BddSourceError.unknown_trace_ids(unknown)
    return source


def render_bdd_source(source: BddScenarioSource) -> str:
    declaration = json.dumps(_validated_source(source), separators=(",", ":"), sort_keys=True)
    return f"<!-- t3-bdd-source {declaration} -->"


def coerce_bdd_source(raw: object) -> BddScenarioSource | None:
    if not isinstance(raw, dict):
        return None
    raw_ids = raw.get("scenario_ids")
    return {
        "prd_page": str(raw.get("prd_page") or ""),
        "bdd_revision": str(raw.get("bdd_revision") or ""),
        "status": str(raw.get("status") or ""),
        "scenario_ids": [str(item) for item in raw_ids] if isinstance(raw_ids, list) else [],
    }
