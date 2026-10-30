"""Persisted shapes for read-only backlog sweep dispatches and retries."""

from typing import TypedDict


class SweepDispatchWindow(TypedDict):
    task_id: int
    index: list[str]
    examined: list[str]
    external_count: int


class SweepFoldProposalRecord(TypedDict):
    folds: list[dict[str, str]]
    run_id: str


class SweepPendingRun(TypedDict):
    task_id: int
    run_id: str
    examined_count: int
    external_count: int
    folds: list[dict[str, str]]
    gaps: list[tuple[int, list[str]]]
