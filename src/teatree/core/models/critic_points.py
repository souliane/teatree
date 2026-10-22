"""Structured, classified points inside one merge critic item."""

from dataclasses import dataclass

from teatree.core.models.finding_standard import FindingKind, normalize_finding_kind
from teatree.types import RawAPIDict


@dataclass(frozen=True, slots=True)
class CriticPoint:
    kind: str
    summary: str
    citation: str = ""
    file: str = ""
    line: int = 0
    id: str = ""

    @classmethod
    def coerce(cls, raw: RawAPIDict) -> "CriticPoint":
        summary = str(raw.get("summary") or "").strip()
        kind = (
            normalize_finding_kind(str(raw.get("kind") or ""), str(raw.get("severity") or ""))
            if summary
            else FindingKind.BLOCKING
        )
        line = raw.get("line")
        return cls(
            kind=kind,
            summary=summary,
            citation=str(raw.get("citation") or "").strip(),
            file=str(raw.get("file") or "").strip(),
            line=line if isinstance(line, int) and line > 0 else 0,
            id=str(raw.get("id") or ""),
        )

    def as_dict(self) -> dict[str, str | int]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "citation": self.citation,
            "file": self.file,
            "line": self.line,
            "id": self.id,
        }


@dataclass(frozen=True, slots=True)
class PriorFindingResolution:
    finding_id: str
    status: str
    citation: str

    @classmethod
    def coerce(cls, raw: RawAPIDict) -> "PriorFindingResolution":
        return cls(
            finding_id=str(raw.get("finding_id") or "").strip(),
            status=str(raw.get("status") or "").strip().lower(),
            citation=str(raw.get("citation") or "").strip(),
        )

    def as_dict(self) -> dict[str, str]:
        return {"finding_id": self.finding_id, "status": self.status, "citation": self.citation}
