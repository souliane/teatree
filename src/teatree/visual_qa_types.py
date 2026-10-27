"""Serialized visual QA report shapes used by ticket and ship gates."""

from typing import TypedDict


class VisualQAPageError(TypedDict):
    kind: str
    message: str


class VisualQAPageDetail(TypedDict):
    url: str
    errors: list[VisualQAPageError]


class VisualQASummary(TypedDict, total=False):
    repo_path: str
    head_sha: str
    targets: list[str]
    skipped_reason: str
    not_run_reason: str
    base_url: str
    pages_checked: int
    errors: int
    details: list[VisualQAPageDetail]
    unchecked_reasons: dict[str, str]
    report_markdown: str
    bypass: dict[str, str]
