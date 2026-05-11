"""Scan PRs the active user has open across configured code-host repos."""

from dataclasses import dataclass
from typing import cast

from teatree.backends.protocols import CodeHostBackend
from teatree.core.sync import RawAPIDict
from teatree.loop.scanners.base import ScanSignal, SignalPayload


def _str_field(data: RawAPIDict, *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            return value
    return ""


def _int_field(data: RawAPIDict, *names: str) -> int:
    for name in names:
        value = data.get(name)
        if isinstance(value, int):
            return value
    return 0


def _pipeline_status(pr: RawAPIDict) -> str:
    """Return the most relevant pipeline state across host shapes.

    GitLab MRs expose ``head_pipeline.status``; GitHub PRs expose a
    nested ``status_check_rollup`` or ``mergeable_state``. Scanners
    surface whatever the backend chose to populate; missing data is "".
    """
    pipeline = pr.get("head_pipeline")
    if isinstance(pipeline, dict):
        status = cast("RawAPIDict", pipeline).get("status")
        if isinstance(status, str):
            return status
    rollup = pr.get("status_check_rollup")
    if isinstance(rollup, dict):
        state = cast("RawAPIDict", rollup).get("state")
        if isinstance(state, str):
            return state
    state = pr.get("mergeable_state")
    return state if isinstance(state, str) else ""


@dataclass(slots=True)
class MyPrsScanner:
    """Lists open PRs authored by the active user.

    Returns a ``my_pr.failed`` signal when the head pipeline is in a
    failed state, ``my_pr.draft_notes`` when there are pending review
    comments to address, and ``my_pr.open`` for every other open PR so
    the dispatcher can render an "in flight" summary.
    """

    host: CodeHostBackend
    name: str = "my_prs"

    def scan(self) -> list[ScanSignal]:
        author = self.host.current_user()
        if not author:
            return []
        prs = self.host.list_my_prs(author=author)
        signals: list[ScanSignal] = []
        for pr in prs:
            url = _str_field(pr, "web_url", "html_url")
            title = _str_field(pr, "title")
            iid = _int_field(pr, "iid", "number")
            status = _pipeline_status(pr)
            base_payload: SignalPayload = {
                "url": url,
                "title": title,
                "iid": iid,
                "status": status,
                "raw": pr,
            }
            if status in {"failed", "failure", "error"}:
                signals.append(
                    ScanSignal(
                        kind="my_pr.failed",
                        summary=f"PR #{iid} pipeline {status}: {title}",
                        payload=base_payload,
                    )
                )
                continue
            draft_count = _int_field(pr, "user_notes_count", "review_comments")
            if draft_count > 0 and status != "success":
                signals.append(
                    ScanSignal(
                        kind="my_pr.draft_notes",
                        summary=f"PR #{iid} has {draft_count} unresolved notes: {title}",
                        payload={**base_payload, "draft_count": draft_count},
                    )
                )
                continue
            signals.append(
                ScanSignal(
                    kind="my_pr.open",
                    summary=f"PR #{iid} {status or 'open'}: {title}",
                    payload=base_payload,
                )
            )
        return signals
