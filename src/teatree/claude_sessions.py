"""Discover and list Claude Code conversation sessions.

Reads ``~/.claude/history.jsonl`` and ``~/.claude/projects/`` to build a
deterministic session index. Detects whether each session finished normally
or was interrupted (killed process, disconnected terminal, etc.).
"""

import json
import operator
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """A single Claude conversation session."""

    session_id: str
    project: str
    first_prompt: str
    timestamp: int | float
    mtime: float
    cwd: str
    status: str  # "finished", "interrupted", "active", "unknown"


def _session_end_status(conv_file: Path) -> str:
    """Determine how a session ended by reading the last JSONL entry.

    Returns ``"finished"``, ``"interrupted"``, ``"active"``, or ``"unknown"``.
    """
    try:
        raw = conv_file.read_bytes()
    except OSError:
        return "unknown"

    end = raw.rstrip()
    idx = end.rfind(b"\n")
    last_line = end[idx + 1 :] if idx >= 0 else end
    try:
        entry = json.loads(last_line)
    except (json.JSONDecodeError, ValueError):
        return "unknown"

    if entry.get("type") == "last-prompt":
        return "finished"

    if _is_session_running(conv_file.stem):
        return "active"

    return "interrupted"


def _is_session_running(session_id: str) -> bool:
    """Check if a Claude session PID is still alive."""
    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return False
    for sf in sessions_dir.glob("*.json"):
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("sessionId") != session_id:
            continue
        try:
            os.kill(data["pid"], 0)
        except (ProcessLookupError, PermissionError, KeyError):
            return False
        else:
            return True
    return False


def _build_session_index(history_file: Path) -> dict[str, dict]:
    """Build a mapping of sessionId -> {first_prompt, timestamp, project} from history."""
    index: dict[str, dict] = {}
    if not history_file.is_file():
        return index

    with history_file.open(encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            sid = obj.get("sessionId", "")
            if sid and sid not in index:
                index[sid] = {
                    "first_prompt": obj.get("display", "")[:120],
                    "timestamp": obj.get("timestamp", 0),
                    "project": obj.get("project", ""),
                }
    return index


def _extract_first_user_message(conv_file: Path) -> tuple[str, int]:
    """Read the first user message and its timestamp from a conversation JSONL."""
    try:
        with conv_file.open(encoding="utf-8") as f:
            for jline in f:
                entry = _safe_json(jline)
                if entry is None or entry.get("type") != "user":
                    continue
                return _parse_user_content(entry), entry.get("timestamp", 0)
    except OSError:
        pass
    return "", 0


def _parse_user_content(entry: dict) -> str:
    """Extract text content from a user message entry."""
    msg = entry.get("message", {})
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content[:120]
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")[:120]
    return ""


def _safe_json(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


@dataclass(frozen=True, slots=True)
class SessionQuery:
    """Parameters for listing sessions."""

    projects_dir: Path | None = None
    history_file: Path | None = None
    cwd: str = ""
    project_filter: str = ""
    all_projects: bool = False
    limit: int = 20


def list_sessions(query: SessionQuery | None = None, /, **kwargs: object) -> list[SessionInfo]:
    """Return a list of session info sorted by most recent first."""
    if query is None:
        query = SessionQuery(**kwargs)  # type: ignore[arg-type]

    claude_home = Path.home() / ".claude"
    projects_dir = query.projects_dir or claude_home / "projects"
    history_file = query.history_file or claude_home / "history.jsonl"

    if not projects_dir.is_dir():
        return []

    session_index = _build_session_index(history_file)

    cwd = query.cwd or str(Path.cwd())
    cwd_key = cwd.replace("/", "-").lstrip("-")

    results: list[SessionInfo] = []
    for project_path in sorted(projects_dir.iterdir()):
        if not project_path.is_dir():
            continue
        dir_name = project_path.name

        if not _matches_filter(dir_name, cwd_key, query.project_filter, all_projects=query.all_projects):
            continue

        results.extend(
            _build_session_info(conv_file, dir_name, session_index)
            for conv_file in sorted(project_path.glob("*.jsonl"), key=_file_mtime, reverse=True)
        )

    results.sort(key=operator.attrgetter("mtime"), reverse=True)
    return results[: query.limit]


def _matches_filter(dir_name: str, cwd_key: str, project_filter: str, *, all_projects: bool) -> bool:
    if all_projects:
        return True
    if project_filter:
        return project_filter in dir_name
    return cwd_key in dir_name


def _file_mtime(p: Path) -> float:
    return p.stat().st_mtime


def _build_session_info(conv_file: Path, dir_name: str, session_index: dict[str, dict]) -> SessionInfo:
    sid = conv_file.stem
    info = session_index.get(sid)

    if info:
        first_prompt = info["first_prompt"]
        ts = info["timestamp"]
        session_cwd = info["project"]
    else:
        first_prompt, ts = _extract_first_user_message(conv_file)
        session_cwd = ""

    home = str(Path.home())
    if session_cwd:
        label = session_cwd.replace(home, "~") if session_cwd.startswith(home) else session_cwd
    else:
        label = dir_name

    mtime = conv_file.stat().st_mtime
    return SessionInfo(
        session_id=sid,
        project=label,
        first_prompt=first_prompt,
        timestamp=ts or int(mtime * 1000),
        mtime=mtime,
        cwd=session_cwd,
        status=_session_end_status(conv_file),
    )
