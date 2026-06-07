"""Resolve which on-disk session JSONL to read, scoped to the cwd's project.

Pure file-resolution, no typer/CLI dependency, so both the
``t3 eval transcript-replay`` command and the ``t3 <overlay> retro gate-failures``
command resolve a session the same way without one importing the other's CLI
package. Lives in ``teatree.eval`` (layer ``integration``) so a lower CLI command
and a core management command can both reach it on a forward edge.
"""

from pathlib import Path

from teatree.claude_sessions import list_sessions


def resolve_transcript(*, latest: bool, session: str | None, file: Path | None) -> Path | None:
    """Resolve which on-disk session JSONL to replay, or ``None`` when none found.

    Scoped to the current project slug (the cwd-derived project directory) so
    the replay never reads another project's logs. ``--file`` wins; then
    ``--session`` looks up a session id within scope; otherwise the most recent
    session for the cwd's project is replayed when ``--latest`` (the default).
    ``--no-latest`` with no ``--session``/``--file`` resolves to nothing.
    """
    if file is not None:
        return file if file.is_file() else None
    if session is not None:
        match = next((s for s in list_sessions(limit=200) if s.session_id == session), None)
    elif latest:
        sessions = list_sessions(limit=200)
        match = sessions[0] if sessions else None
    else:
        match = None
    if match is None:
        return None
    projects_dir = Path.home() / ".claude" / "projects"
    for project_path in projects_dir.iterdir() if projects_dir.is_dir() else []:
        candidate = project_path / f"{match.session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None
