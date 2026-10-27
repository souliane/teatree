"""Structured findings and the shared blocking versus NIT review standard."""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NotRequired, TypedDict

from django.db import models

from teatree.core.modelkit.diff_scope import BLOCKING_SEVERITIES

if TYPE_CHECKING:
    from collections.abc import Mapping


class FindingKind(StrEnum):
    BLOCKING = "blocking"
    NIT = "nit"
    NOTE = "note"


FINDING_STANDARD = (
    "BLOCKING: wrong behaviour, a false statement about current behaviour, shipped behaviour no test pins, "
    "a vacuous test, a test shown redundant by mutation, a duplicated definition introduced by the diff, "
    "or a red required check. NIT: wording, naming, comment placement, ordering, missing docstrings, or style. "
    "NOTE: a venue cannot read or run a check, or documentation suggests a defect without executed reproduction. "
    "Only BLOCKING findings may fail a critic item or hold a review."
)


def normalize_finding_kind(kind: str, severity: str) -> str:
    normalized = kind.strip().lower()
    if severity.strip().lower() in BLOCKING_SEVERITIES:
        return FindingKind.BLOCKING
    if normalized == FindingKind.NIT:
        return FindingKind.NIT
    return FindingKind.BLOCKING


class FindingDict(TypedDict):
    """The JSONField-serialised shape of one :class:`Finding`."""

    severity: str
    summary: str
    file: str
    line: int
    kind: str
    basis: NotRequired[str]
    citation: NotRequired[str]
    reproduction: NotRequired[str]


def _coerce_line(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


@dataclass(frozen=True, slots=True)
class Finding:
    """One structured cold-review finding: severity + ``file:line`` + summary."""

    severity: str
    summary: str
    file: str = ""
    line: int = 0
    kind: str = ""
    basis: str = ""
    citation: str = ""
    reproduction: str = ""

    def __post_init__(self) -> None:
        kind = normalize_finding_kind(self.kind, self.severity)
        if (
            kind == FindingKind.BLOCKING
            and not self.reproduction.strip()
            and self.basis in {"documentation", "venue_limit"}
        ):
            kind = FindingKind.NOTE
            object.__setattr__(self, "severity", "note")
        object.__setattr__(self, "kind", kind)

    def as_dict(self) -> FindingDict:
        data: FindingDict = {
            "severity": self.severity,
            "summary": self.summary,
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
        }
        if self.basis:
            data["basis"] = self.basis
        if self.citation:
            data["citation"] = self.citation
        if self.reproduction:
            data["reproduction"] = self.reproduction
        return data

    @classmethod
    def from_dict(cls, raw: "Mapping[str, object]") -> "Finding":
        return cls(
            severity=str(raw.get("severity", "")),
            summary=str(raw.get("summary", "")),
            file=str(raw.get("file", "")),
            line=_coerce_line(raw.get("line")),
            kind=str(raw.get("kind", "")),
            basis=str(raw.get("basis", "")),
            citation=str(raw.get("citation", "")),
            reproduction=str(raw.get("reproduction") or ""),
        )

    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        return self.file or "(MR-level)"

    def describe(self) -> str:
        return f"[{self.severity}] {self.location()} — {self.summary}"


class Severity(models.TextChoices):
    BLOCKER = "blocker", "Blocker"
    MAJOR = "major", "Major"
    MINOR = "minor", "Minor"
    NIT = "nit", "Nit"
