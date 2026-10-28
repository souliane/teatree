from typing import TypedDict


class DreamGapEntry(TypedDict, total=False):
    gap_key: str
    cluster_key: str
    title: str
    citation: str
    theme: str
    detail: str
    declined_hosts: list[int]
    offered_at: str


class DreamGapDisposition(TypedDict):
    disposition: str
    evidence: str


class DreamGapRelease(TypedDict):
    at: str
    reason: str
    batch: list[DreamGapEntry]
    claimed_delivered: list[str]


class DreamFunnelEntry(TypedDict):
    at: str
    rows: int
    kept: int
    core_gaps: int
    withheld: int
    pending: int
    folded: int
    addressed: int
    rejected: int
    retired: int
    stranded_hosts: int
    orphans: int
