"""Workspace orphan listing for the CLI and session-end hook."""

from typing import IO, TypedDict, cast

from django.core.management.base import OutputWrapper

from teatree.core.gates.orphan_guard import find_orphans_in_workspace
from teatree.core.machine_output import emit
from teatree.core.models import Worktree


class OrphanEntry(TypedDict):
    repo: str
    branch: str
    status: str
    ahead_count: int


def list_orphan_entries(*, branch: str | None = None) -> list[OrphanEntry]:
    reports = (
        find_orphans_in_workspace(rows=Worktree.objects.filter(branch=branch))
        if branch is not None
        else find_orphans_in_workspace()
    )
    return [
        OrphanEntry(repo=r.repo, branch=r.branch, status=r.status.value, ahead_count=r.ahead_count) for r in reports
    ]


def render_orphans(entries: list[OrphanEntry]) -> str:
    lines = "".join(f"{e['repo']} ({e['branch']}) — {e['ahead_count']} ahead, {e['status']}\n" for e in entries)
    return lines or "No orphan branches.\n"


def run_orphan_listing(stdout: OutputWrapper, stderr: OutputWrapper, *, branch: str | None, json_output: bool) -> None:
    entries = list_orphan_entries(branch=branch)
    emit(
        entries,
        json_output=json_output,
        out=cast("IO[str]", stdout),
        err=cast("IO[str]", stderr),
        human=render_orphans(entries),
    )
