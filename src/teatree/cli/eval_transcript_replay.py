"""Resolve which on-disk session transcript ``t3 eval transcript-replay`` reads.

The replay command itself lives in :mod:`teatree.cli.eval`; this module holds
the file-resolution concern (``--file`` / ``--session`` / ``--latest`` scoped to
the cwd's project) so the command body stays a thin coordinator.
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
