"""Typed response shapes for backend APIs.

These TypedDicts document the known shapes of responses from backend
protocol implementations.  They do NOT replace the Protocol signatures
(which remain ``dict[str, object]`` for structural typing compatibility).
"""

from typing import TypedDict


class MergeRequestResponse(TypedDict, total=False):
    iid: int
    web_url: str
    title: str
    source_branch: str
    target_branch: str
    error: str


class PipelineResponse(TypedDict, total=False):
    id: int
    status: str
    web_url: str
    ref: str
    error: str


class QualityCheckResponse(TypedDict, total=False):
    pipeline_id: int
    status: str
    total_count: int
    success_count: int
    failed_count: int
    error_count: int
    error: str


class NoteResponse(TypedDict, total=False):
    id: int
    body: str
    error: str


class UploadResponse(TypedDict, total=False):
    url: str
    markdown: str
    error: str


class IssueResponse(TypedDict, total=False):
    iid: int
    title: str
    description: str
    state: str


class ChatResponse(TypedDict, total=False):
    ok: bool
    channel: str
    ts: str
