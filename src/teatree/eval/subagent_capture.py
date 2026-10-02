"""Locate the JSONL Claude Code wrote for a just-dispatched sub-agent.

The ``/t3:running-evals`` skill dispatches one in-session ``Agent`` per scenario
to produce a subscription-covered transcript. Claude Code writes each sub-agent's
trajectory to ``~/.claude/projects/<slug>/<session-id>/subagents/agent-<id>.jsonl``.
This module finds the freshest such file (optionally one written after a recorded
timestamp, to disambiguate sequential dispatches) and validates it is a real
sub-agent transcript before the skill copies it to the scenario's expected path.

Selection is by modification time across every project slug, because a dispatched
sub-agent may run with a different cwd than the driver session. This module only
reads and copies on-disk files — it never invokes ``claude -p`` or the Agent SDK,
so the subscription lane stays unmetered.
"""

import dataclasses
import shutil
from pathlib import Path

from teatree.eval import transcript_manifest
from teatree.eval.subagent_transcript import is_subagent_transcript


@dataclasses.dataclass(frozen=True)
class SubagentFile:
    path: Path
    mtime: float


@dataclasses.dataclass(frozen=True)
class CaptureProvenance:
    """The scenario identity a capture binds its transcript to (its manifest sidecar)."""

    scenario: str
    prompt: str
    head_sha: str


def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def discover_subagent_files(*, since: float | None = None, projects_dir: Path | None = None) -> list[SubagentFile]:
    root = projects_dir or _projects_dir()
    if not root.is_dir():
        return []
    found = [
        SubagentFile(path=path, mtime=path.stat().st_mtime)
        for path in root.glob("*/*/subagents/agent-*.jsonl")
        if path.is_file() and (since is None or path.stat().st_mtime >= since)
    ]
    found.sort(key=lambda f: f.mtime, reverse=True)
    return found


def newest_subagent_transcript(*, since: float | None = None, projects_dir: Path | None = None) -> Path | None:
    for candidate in discover_subagent_files(since=since, projects_dir=projects_dir):
        if is_subagent_transcript(candidate.path.read_text(encoding="utf-8", errors="replace")):
            return candidate.path
    return None


def capture_to(
    target: Path,
    *,
    since: float | None = None,
    projects_dir: Path | None = None,
    provenance: CaptureProvenance | None = None,
) -> Path | None:
    """Copy the freshest sub-agent transcript to *target*, writing its provenance sidecar.

    When *provenance* is supplied (the ``t3 eval capture-subagent`` path always
    supplies it), a :mod:`teatree.eval.transcript_manifest` sidecar is written next
    to *target* so the grade step can refuse a stale or cross-contaminated
    transcript. Omitting it (a bare copy) writes no sidecar — the transcript then
    grades unverified.
    """
    source = newest_subagent_transcript(since=since, projects_dir=projects_dir)
    if source is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if provenance is not None:
        transcript_manifest.write(
            target,
            scenario=provenance.scenario,
            prompt=provenance.prompt,
            head_sha=provenance.head_sha,
            source=source,
        )
    return source
