"""Track ``CronCreate`` / ``CronDelete`` / ``ScheduleWakeup`` for the statusline (#2384 PR-09).

A ``PostToolUse`` recorder — NOT a deny gate. It persists per-session cron/loop
metadata (job name, cadence, wakeup) to the ``<session>.crons`` state file that
the statusline reads, deriving a short readable loop name from the cron/loop
prompt. Purely observational: it never blocks a tool call and fails silent.

Extracted whole from ``hook_router`` (the #2384 Wave-2 router split) so the
dispatcher shrinks; the router re-exports :func:`handle_track_cron_jobs`,
:func:`derive_loop_name`, and :func:`cron_cadence_seconds` into ``_HANDLERS`` /
its own namespace unchanged. The per-session state helpers (``_ensure_state_dir``
/ ``_state_file``, bound to the router's ``STATE_DIR``) and the loop-prompt
constant ``_LOOP_PROMPT`` stay in the router and are back-imported lazily inside
the handler, so a test patching ``router.STATE_DIR`` still steers this handler.

Cold-import safe: the live hook is a bare ``python3`` subprocess with no
guarantee ``teatree`` is importable, so the module top imports only stdlib plus
the already-extracted ``loop_registrations`` sibling — never Django / ``teatree``.
"""

import json
import time
from pathlib import Path

from hooks.scripts.loop_registrations import loop_name_from_prompt

_LOOP_NAME_MAX = 20
_CRON_FIELD_COUNT = 5
_T3_LOOP_SUBCOMMAND_PARTS = 2


def clean_token(token: str) -> str:
    """Strip surrounding/trailing punctuation and backticks from a token."""
    return token.strip("`").strip(".,;:!?\"'()[]{}/").strip("`")


def derive_loop_name(prompt: str) -> str:
    """Derive a short display name from a cron/loop prompt.

    - The canonical teatree loop prompt maps to a stable readable name.
    - Slash-command prompts use the command token.
    - Otherwise a short label is taken from the first meaningful word.

    Surrounding punctuation and backticks are always stripped.
    """
    from hooks.scripts.hook_router import _LOOP_PROMPT  # noqa: PLC0415 deferred back-import

    prompt = prompt.strip()

    # 1. A teatree loop-tick prompt → a stable readable name: a per-loop tick
    # (#2650) shows that loop's OWN name (the native `/loop` it drives), the
    # legacy fat-tick prompt shows "tick".
    per_loop = loop_name_from_prompt(prompt)
    if per_loop is not None or prompt == _LOOP_PROMPT or prompt.startswith(_LOOP_PROMPT):
        return (per_loop or "tick")[:_LOOP_NAME_MAX]

    if prompt.startswith("!"):
        prompt = prompt[1:].strip()

    parts = prompt.split()
    if not parts:
        return "loop"

    # `t3 loop <subcommand>` shell form → the subcommand (e.g. `tick`).
    if parts[:2] == ["t3", "loop"] and len(parts) > _T3_LOOP_SUBCOMMAND_PARTS:
        return clean_token(parts[2])[:_LOOP_NAME_MAX] or "loop"

    # 2. Slash-command form: a leading `/foo` or an embedded `/foo` token.
    #    `/loop 5m /babysit-prs` wraps the real command — use the last token.
    slash_tokens = [p for p in parts if p.startswith("/") and len(p) > 1]
    if slash_tokens:
        return clean_token(slash_tokens[-1].split("/")[-1])[:_LOOP_NAME_MAX] or "loop"

    # 3. Prose: first meaningful word, punctuation/backticks stripped.
    for part in parts:
        cleaned = clean_token(part)
        if cleaned:
            return cleaned[:_LOOP_NAME_MAX]
    return "loop"


def load_crons(path: Path) -> dict:
    if not path.is_file():
        return {"jobs": {}, "wakeup": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"jobs": {}, "wakeup": None}


def save_crons(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def handle_track_cron_jobs(data: dict) -> None:
    """Track CronCreate/CronDelete/ScheduleWakeup for statusline display."""
    from hooks.scripts.hook_router import _ensure_state_dir, _state_file  # noqa: PLC0415 deferred back-import

    tool_name = data.get("tool_name", "")
    if tool_name not in {"CronCreate", "CronDelete", "ScheduleWakeup"}:
        return

    session_id = data.get("session_id", "")
    if not session_id:
        return

    _ensure_state_dir()
    crons_file = _state_file(session_id, "crons")
    state = load_crons(crons_file)
    if "jobs" not in state:
        state["jobs"] = {}

    now = int(time.time())
    tool_input = data.get("tool_input", {})

    if tool_name == "CronCreate":
        prompt = tool_input.get("prompt", "")
        cron_expr = tool_input.get("cron", "")
        name = derive_loop_name(prompt)
        job_id = data.get("tool_result", {}).get("id", "") or f"job-{now}"
        cadence = cron_cadence_seconds(cron_expr)
        state["jobs"][job_id] = {
            "name": name,
            "cron": cron_expr,
            "cadence": cadence,
            "created_at": now,
        }
        _state_file(session_id, "loop-pending").unlink(missing_ok=True)
    elif tool_name == "CronDelete":
        job_id = tool_input.get("id", "")
        state["jobs"].pop(job_id, None)
    elif tool_name == "ScheduleWakeup":
        delay = int(tool_input.get("delaySeconds", 0))
        reason = tool_input.get("reason", "")
        state["wakeup"] = {
            "name": reason[:30] if reason else "loop",
            "next_epoch": now + delay,
        }

    save_crons(crons_file, state)


def cron_cadence_seconds(cron_expr: str) -> int | None:
    """Extract cadence in seconds from simple */N minute patterns."""
    parts = cron_expr.strip().split()
    if len(parts) != _CRON_FIELD_COUNT:
        return None
    minute = parts[0]
    if minute.startswith("*/") and all(p == "*" for p in parts[1:]):
        try:
            return int(minute[2:]) * 60
        except ValueError:
            return None
    return None
