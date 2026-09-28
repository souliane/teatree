"""PreToolUse: block-raw-ticket-ignore-loop (#2663 dream-batch dea750a552f8f2d2).

``teatree.core.gates.bulk_close_gate`` enforces the no-bulk-close rule for the
gated ``t3 <overlay> ticket bulk-close`` command, but its own docstring names the
residual it cannot close on its own: an agent invoking the single-item
``t3 <overlay> ticket transition <id> ignore`` command directly, N times in a
row, is "the CLI's own boundary, not a hole this gate can close on its own."
This hook closes that boundary at the Bash PreToolUse layer: a session that
runs the raw per-item ignore command :data:`RAW_IGNORE_LOOP_THRESHOLD` or more
times is told to use ``ticket bulk-close`` instead — a single or double
legitimate use stays allowed, since the whole point of the threshold is not to
false-positive on an operator retiring one or two stray rows by hand.

State: a per-session ``<session>.ticket-ignore-count`` line-count file, one
line appended per matching invocation — the same ``STATE_DIR`` pattern
``deny_circuit_breaker`` uses for its streak. It never resets within the
session: once tripped, every further raw ignore call stays denied, mirroring
``bulk_close_gate``'s own "no partial credit" posture — there is no legitimate
reason to keep looping the raw command after being told to switch.

The deny routes through the router's shared ``_fail_open_or_deny`` chokepoint
(back-imported lazily), so the self-rescue allowlist, the master fail-open
switch, and the repeated-denial circuit breaker all apply — this gate is
narrow (it matches only one specific command shape, never arbitrary Bash) and
carries its own never-lockout escapes for free.

Cold-import safe: the live PreToolUse hook is a bare ``python3`` subprocess with
no guarantee ``teatree`` is importable, so the module top imports only stdlib
and the already-extracted ``state_files`` sibling — never Django / ``teatree.core``.
"""

import re
import sys
from pathlib import Path

from hooks.scripts.state_files import append_line as _append_line
from hooks.scripts.state_files import read_lines as _read_lines

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object —
# the same pattern ``direct_command_guard`` uses.
sys.modules.setdefault("raw_ticket_ignore_loop_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.raw_ticket_ignore_loop_gate", sys.modules[__name__])

#: The count at which a session is told to switch to ``ticket bulk-close`` — the
#: 3rd raw ``ticket transition <id> ignore`` invocation, and every one after it.
RAW_IGNORE_LOOP_THRESHOLD = 3

#: Anchored to the command's start (optionally behind env-var prefixes), so the
#: phrase merely quoted in an ``echo``/commit message/doc string is text, not an
#: invocation — the same anchoring ``direct_command_guard``'s ``_T3_CMD_PREFIX_RE``
#: uses. The overlay name sits between ``t3`` and ``ticket``; the ticket id is any
#: non-space token so a numeric id or a quoted variable both match.
_TICKET_IGNORE_RE = re.compile(r"^(?:\w+=\S+\s+)*t3\s+\S+\s+ticket\s+transition\s+\S+\s+ignore\b")

_DENY_REASON = (
    "BLOCKED: this is raw `ticket transition <id> ignore` call #{count} this session — "
    "retiring several tickets one at a time is exactly the per-item loop "
    "`bulk_close_gate` exists to close. Use `t3 <overlay> ticket bulk-close` instead, "
    "which enforces the same threshold with an explicit per-item confirmation."
)


def raw_ticket_ignore_match(command: str) -> bool:
    """Whether *command* invokes the raw single-item ``ticket transition <id> ignore``."""
    return bool(_TICKET_IGNORE_RE.match(command.strip()))


def _ignore_count_file(session_id: str) -> Path:
    from hooks.scripts.hook_router import STATE_DIR  # noqa: PLC0415 deferred back-import

    return STATE_DIR / f"{session_id}.ticket-ignore-count"


def handle_block_raw_ticket_ignore_loop(data: dict) -> bool:
    """Block a session's Nth+ raw ``ticket transition <id> ignore`` Bash call.

    Returns True when a deny was emitted (caller should stop the handler chain).
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    if not raw_ticket_ignore_match(command):
        return False
    session_id = data.get("session_id", "")
    if not session_id:
        return False
    state = _ignore_count_file(session_id)
    count = len(_read_lines(state)) + 1
    _append_line(state, "1")
    if count < RAW_IGNORE_LOOP_THRESHOLD:
        return False
    return _fail_open_or_deny(data, _DENY_REASON.format(count=count), gate_id="raw_ticket_ignore_loop")
