"""Session history view — reads Claude JSONL transcripts."""

import json
import re
from pathlib import Path

from django.http import Http404, HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.views import View

_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _project_dir_for_cwd(cwd: str) -> Path:
    """Convert a cwd path to Claude's project directory name."""
    return _CLAUDE_PROJECTS_DIR / cwd.replace("/", "-")


def _extract_text(content: object) -> str:
    """Extract displayable text from a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))  # ty: ignore[no-matching-overload]
            if block_type == "text":
                parts.append(str(block.get("text", "")))  # ty: ignore[no-matching-overload]
            elif block_type == "tool_use":
                parts.append(f"[tool: {block.get('name', '')}]")  # ty: ignore[no-matching-overload]
        return "\n".join(parts)
    return ""


def _load_transcript(session_id: str, cwd: str) -> list[dict[str, str]]:
    """Load a session transcript from the JSONL file."""
    project_dir = _project_dir_for_cwd(cwd)
    jsonl_path = project_dir / f"{session_id}.jsonl"
    if not jsonl_path.is_file():
        return []

    messages: list[dict[str, str]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = data.get("type", "")
        if msg_type not in {"user", "assistant"}:
            continue

        message = data.get("message", {})
        role = message.get("role", msg_type)
        text = _extract_text(message.get("content", ""))
        if text.strip():
            messages.append({"role": role, "text": text[:5000]})

    return messages


class SessionHistoryView(View):
    def get(self, request: HttpRequest, session_id: str) -> HttpResponse:
        cwd = request.GET.get("cwd", "")
        if not cwd or not re.match(r"^[/\w.-]+$", cwd):
            raise Http404

        messages = _load_transcript(session_id, cwd)

        return TemplateResponse(
            request,
            "teatree/partials/session_history.html",
            {"messages": messages, "session_id": session_id},
        )
