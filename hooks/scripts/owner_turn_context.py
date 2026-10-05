"""PostToolUse: the owner's new prompt gets its archived-memory recall and the standing directives now due.

Nothing runs on ``UserPromptSubmit``, so a prompt never waits on teatree; the first tool call after an
owner prompt carries what a prompt hook used to (#2746, #4166). This is its own process, never the
router: Django-free, silent for a subagent's call, and bounded by :data:`BUDGET_SECONDS` until its
cursor has moved — an overrun or any error before the context is written exits 0 having written
nothing. Under an exclusive lock on ``<session>.owner-turn-cursor`` it reads only the newest end of the
session log appended since the previous call, so each owner prompt is served at most once, as ONE nested
``additionalContext`` object. The prompt and the directives are recorded as delivered only after that
object was written and flushed inside the budget, so a write or flush the budget or a closed pipe cuts
short, or a cursor write the budget cuts short, is delivered again by the next call.
"""

import fcntl
import json
import logging
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TextIO

# Run as a script, the plugin root and its ``src/`` are not on ``sys.path``; both imports below need them.
if str(Path(__file__).resolve().parents[2] / "src") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.scripts.additional_context import emit_additional_context
from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent
from hooks.scripts.owner_prompts import UnreadPrompts, owner_prompts_since
from hooks.scripts.standing_directives_delivery import DueRules, due_rules
from hooks.scripts.state_files import hook_state_dir
from teatree.loops.dream import recall

BUDGET_SECONDS = 0.3
CURSOR_SUFFIX = "owner-turn-cursor"

logger = logging.getLogger(__name__)


class OverBudgetError(Exception):
    """The call ran past :data:`BUDGET_SECONDS`."""


def _budget_spent(_signum: int, _frame: FrameType | None) -> None:
    raise OverBudgetError


def _project_memory_dir(data: dict, cold_index_name: str) -> Path | None:
    """Resolve the project's memory dir holding the cold index, or ``None``.

    PRIMARY: the session log's sibling ``memory/`` dir (``Path(transcript_path).parent / "memory"``).
    FALLBACK: the ``~/.claude/projects/<cwd-slug>/memory`` dir, deriving the slug from ``cwd``
    (``/`` → ``-``) the way the harness names project dirs. Each candidate is accepted only if it
    actually holds the cold index file; otherwise ``None`` (inject nothing — silent degrade).
    """
    transcript_path = data.get("transcript_path", "")
    if isinstance(transcript_path, str) and transcript_path:
        candidate = Path(transcript_path).parent / "memory"
        if (candidate / cold_index_name).is_file():
            return candidate
    cwd = data.get("cwd", "")
    if isinstance(cwd, str) and cwd:
        candidate = Path.home() / ".claude" / "projects" / cwd.replace("/", "-") / "memory"
        if (candidate / cold_index_name).is_file():
            return candidate
    return None


def _recall(data: dict, query: str) -> str:
    memory_dir = _project_memory_dir(data, recall.COLD_INDEX_NAME)
    if memory_dir is None:
        return ""
    return recall.render_recall_block(recall.recall_cold_memory(memory_dir, query))


@dataclass(frozen=True, slots=True)
class OwnerTurn:
    """What the owner's new prompts bring into this turn, and what to mark delivered once it was written."""

    context: str = ""
    unread: UnreadPrompts | None = None
    rules: DueRules | None = None

    def mark_delivered(self) -> None:
        if self.unread is not None:
            self.unread.mark_read()
        if self.rules is not None:
            self.rules.mark_delivered()


def _held(cursor: TextIO) -> bool:
    try:
        fcntl.flock(cursor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _owner_turn(data: dict, session_id: str, cursor: Path) -> OwnerTurn:
    unread = owner_prompts_since(cursor, str(data.get("transcript_path") or ""))
    if not unread.prompts:
        return OwnerTurn(unread=unread)
    rules = due_rules(session_id, every_slot=False)
    parts = (_recall(data, "\n".join(unread.prompts)), rules.text)
    return OwnerTurn("\n\n".join(part for part in parts if part), unread, rules)


@contextmanager
def owner_turn(data: dict) -> Iterator[OwnerTurn]:
    """The turn context for *data*, holding the session's cursor lock until the caller has written it."""
    session_id = str(data.get("session_id") or "")
    if not session_id or call_is_from_subagent(data):
        yield OwnerTurn()
        return
    state = hook_state_dir()
    state.mkdir(parents=True, exist_ok=True)
    cursor = state / f"{session_id}.{CURSOR_SUFFIX}"
    with cursor.open("a") as held:
        yield _owner_turn(data, session_id, cursor) if _held(held) else OwnerTurn()


def _serve() -> None:
    with owner_turn(json.loads(sys.stdin.read())) as turn:
        if turn.context:
            emit_additional_context("PostToolUse", turn.context)
        turn.mark_delivered()


def main() -> None:
    # One shot: the budget runs until the cursor has moved, and an overrun raised even as it is
    # cancelled lands in the outer handler, never in the harness as a failed hook.
    signal.signal(signal.SIGALRM, _budget_spent)
    signal.setitimer(signal.ITIMER_REAL, BUDGET_SECONDS)
    try:
        try:
            _serve()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    except Exception:
        logger.debug("no owner-turn context for this call", exc_info=True)


if __name__ == "__main__":
    main()
