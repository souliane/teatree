"""PreToolUse: refuse an UNBOUNDED orchestrator read — delegate it instead.

The orchestrate-only boundary already has two enforcement points and neither
covers the shape that actually erodes it. The heavy-Bash gate (
``handle_enforce_orchestrator_boundary``) denies commands that are LONG, and its
escape is ``run_in_background: true``. The investigation nudge (#1442,
``orchestrator_investigation_gate``) WARNs on git/CI archaeology and is
structurally forbidden from ever denying. What the operator measured instead was
dozens of inline ``rg`` sweeps, log trawls and ``… --json | python3 -c`` reductions
in one attended session — each of them fast, so the heavy gate passed them, and
each of them spending the one resource the orchestrator cannot get back.

THE LINE: ROUTING READS A FACT; INVESTIGATION BUILDS UNDERSTANDING
-----------------------------------------------------------------
Decided from the command's own shape, which is all a PreToolUse hook can see —
the output does not exist yet, so output size is not measurable and is not used.
Two shapes are refused because neither can be a bounded fact-fetch:

* a SWEEP — a recursive search or tree walk with no ceiling on its result set
    (``rg``, ``grep -r``, ``git grep``, ``find <path>``), carrying none of the three
    bounds: a count bound (``-m``/``--max-count``), a ``| head``/``| tail``, or a named
    single file (a glob is not a name — it is how a tree walk is spelled);
* a PARSE — output piped into a general-purpose interpreter, which is the shape
    of "this was too big to read, so I wrote a program to reduce it".

Everything else routes and is untouched. ``docker ps``, ``git status``,
``gh pr view``, ``t3 … list --json``, ``| jq '.field'``, ``head -50``,
``sed -n '1,80p'`` and a ``grep`` used as a downstream FILTER all answer one
question with a bounded answer — which is why the sweep patterns are anchored to
a command HEAD and never fire after a single ``|``.

THE NATIVE TOOLS SPELL THE SAME SWEEP, SO THEY ARE GATED TOO
------------------------------------------------------------
A gate that reads ``Bash`` alone is routed around by construction: an
orchestrator refused ``Bash(rg …)`` switches to ``Grep(…)`` — the same read,
one tool name over — and never meets the gate again. ``Glob`` is the same
route-around for the refused ``find <path> -name '*.ext'``. Each is gated by
mapping the Bash arm's own bounds onto that tool's parameters, so a shape that
passes in one spelling passes in the other:

* ``Grep`` — bounded by ``head_limit`` (the count bound ``-m``/``| head`` gives
    Bash), or by a ``path`` naming ONE file (the named-file operand). Neither ⇒ a
    tree walk with no ceiling, exactly like a bare ``rg pattern src/``.
* ``Glob`` — has no count parameter, so the PATTERN is its only ceiling. A
    basename carrying a literal stem (``**/settings_editor.py``,
    ``**/*_scanner.py``) names a file family the caller could enumerate in
    advance; a basename that is only wildcard-plus-extension (``**/*.py``,
    ``*``) names every file of a kind in the tree — the ``find . -name '*.py'``
    shape the Bash arm already refuses. Under-blocks a broad-stem pattern
    deliberately: a ``Glob`` returns PATHS rather than content, so a wrong
    refusal costs more than a missed one.

``Read`` is deliberately absent: it reads one named file, which is the bounded
shape ``head -50`` / ``sed -n '1,80p'`` already pass under.

WHY THIS IS NOT AN ARM OF THE HEAVY-BASH GATE
---------------------------------------------
The two gates guard different resources and therefore cannot share an escape.
The heavy gate guards RESPONSIVENESS, so backgrounding the command discharges
it. This one guards CONTEXT, and a backgrounded sweep still lands in the
orchestrator's context the moment its output is collected — so
``run_in_background`` is deliberately NOT an escape here. Nothing the heavy
denylist already covers is re-listed.

SCOPE: THE ORCHESTRATOR ONLY
----------------------------
A sub-agent sweeping is doing its job, so the discriminators are the ones the
sibling gates already share: ``call_is_from_subagent`` (the ``agent_id`` signal),
``session_lane`` (only a positively-identified interactive CLI session someone attends), and
``_teatree_engaged``. No new context detection is invented here.

Fails OPEN throughout: an unreadable lane, an unreadable posture, or any internal
error is not evidence of investigation and ALLOWS.

Cold-import safe: stdlib only at module top plus the two dependency-free sibling
leaves; every router helper is back-imported lazily.
"""

import os
import re
import sys

from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent
from hooks.scripts.session_lane import LANE_INTERACTIVE_CLI, session_lane

# Alias both identities so the handler the router registers and a test patching a
# helper here operate on ONE module object — the pattern every sibling uses.
sys.modules.setdefault("orchestrator_delegation_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.orchestrator_delegation_gate", sys.modules[__name__])

#: The reading tools this gate governs — the shells of one shape, not an allowlist of
#: tools in general. Every other tool passes by construction.
GATED_TOOLS = frozenset({"Bash", "Grep", "Glob"})

#: A command HEAD: start of string, or a separator that begins a new command.
#: A single ``|`` is deliberately absent — a tool downstream of a pipe is a
#: FILTER over an already-bounded output (``docker ps | grep teatree``), not a
#: sweep, and treating it as one would refuse the orchestrator's routing reads.
_HEAD = r"(?:^|[;\n(){}]|&&?|\|\|)\s*(?:\w+=\S+\s+)*(?:(?:command|exec|time|nice)\s+)*"

#: Recursive searches and tree walks — unbounded result sets by construction.
#: ``find`` requires a non-flag operand so ``find --help`` is not a walk.
_SWEEP_RE = re.compile(
    _HEAD + r"(?:rg(?![\w-])|git\s+grep\b|grep\s+(?:-\S+\s+)*(?:--recursive|-\w*[rR]\w*)(?=\s)|find\s+[^-\s]\S*)"
)

#: The extensions a path operand must carry to read as a real file name.
_FILE_EXTENSIONS = "[A-Za-z][A-Za-z0-9]{0,9}"

#: What turns a sweep back into a bounded read — the levers the refusal teaches. The
#: third one is a NAMED FILE operand: a search over one file is bounded by that file, so
#: refusing `rg pattern one_file.py` while `grep -n pattern one_file.py` passes gates the
#: spelling rather than the shape. A glob is deliberately not a name — `find . -name
#: '*.py'` and `rg pattern 'src/**/*.py'` walk a tree — so the operand must carry a real
#: extension and no glob metacharacter.
_BOUND_RE = re.compile(
    r"(?:^|\s)(?:-m\s*\d+(?=\s|$)|--max-count\b)"
    r"|\|\s*(?:head|tail)\b"
    rf"|\s[^\s*?\[\]'\"]+\.(?:{_FILE_EXTENSIONS})(?=\s|$)"
)

#: The same named-file rule as `_BOUND_RE`'s third arm, applied to a tool ARGUMENT
#: rather than to a word inside a command line.
_NAMED_FILE_RE = re.compile(rf"^[^\s*?\[\]'\"]+\.(?:{_FILE_EXTENSIONS})$")

#: A `Glob` basename whose only literal content is an extension — `*`, `*.py`,
#: `*.{ts,tsx}`. It names every file of a kind in the tree, which is the
#: `find <path> -name '*.ext'` walk the Bash arm refuses.
_STEMLESS_GLOB_RE = re.compile(r"^[*?]+(?:\.[\w{},*]+)?$")

#: Output piped into a general-purpose interpreter. A BARE ``python3 -c`` is not
#: matched: it is equally how a tiny value gets computed, and gating it would
#: refuse routing arithmetic. ``jq``/``awk``/``sed`` are likewise absent — they
#: are field extractors, and the ``--json`` reads the orchestrator routes on need
#: them. Only the PIPE says "an output existed that was too big to read".
_PARSE_RE = re.compile(r"(?<!\|)\|(?!\|)\s*(?:(?:command|exec)\s+)*(?:python3?|node|ruby|perl)\s+-")

#: Per-call escape, mirroring the sibling ``[…-ok: <reason>]`` tokens. The leading
#: ``\S`` is what rejects an empty reason.
_DELEGATE_OK_RE = re.compile(r"\[delegate-ok:\s*(\S[^\]]*?)\s*\]")

#: Scanned prefix of the command. Bounded rather than unlimited (the hook has a
#: 30s ceiling), but wide enough for a trailing ``# [delegate-ok: …]`` comment.
_TOKEN_SCAN_CHARS = 16 * 1024

_AGENTS = (
    "Explore (read-only fan-out search — the right one for a code sweep), "
    "t3:debugger (diagnose a failure), t3:bughunter (reproduce a defect), "
    "t3:reviewer (read a diff)"
)

_BASH_BOUNDS = (
    "add `-m <n>` / `--max-count`, pipe into `head`, name the one file you mean "
    "(`rg pattern path/to/file.py`), use `head -50` / `sed -n '1,80p'` for a file, or "
    "`<cmd> --json | jq '.field'`"
)

_GREP_BOUNDS = "pass `head_limit: <n>`, or point `path` at the ONE file you mean (`path: 'src/teatree/core/models.py'`)"

_GLOB_BOUNDS = (
    "name the file family you are after instead of every file of a kind "
    "(`**/settings_editor.py`, `**/*_scanner.py` — not `**/*.py`)"
)

_TOOL_BOUNDS = {"Grep": _GREP_BOUNDS, "Glob": _GLOB_BOUNDS}


def refusal(shape: str, tool_name: str = "Bash") -> str:
    return (
        f"REFUSED: this is {shape} run inline by the ORCHESTRATOR. It has no ceiling on what it "
        "returns, so it spends the one resource the orchestrator cannot get back — its own "
        "context — on work a sub-agent exists to do.\n"
        f"DELEGATE it: dispatch {_AGENTS} with Agent/Task and read the returned conclusion. "
        "The sub-agent reads the whole tree; you read one answer.\n"
        f"Or BOUND it, if this is one fact you need to ROUTE: {_TOOL_BOUNDS.get(tool_name, _BASH_BOUNDS)}. "
        "A bounded read is a routing read and is never gated.\n"
        "Orchestration is untouched — this gate reads the three READING tools only, so Agent/Task "
        "dispatch, Read, TaskCreate/TaskUpdate, SendMessage, AskUserQuestion and every MCP connector "
        "always pass, as do `git status`, `docker ps`, `gh pr view`, `t3 ... list --json`, and a `grep` "
        "used as a downstream filter.\n"
        "Note `run_in_background: true` is NOT an escape here: this gate guards context, not "
        "responsiveness, and a backgrounded sweep still lands in your context when you collect it.\n"
        "Per-call escape: put `[delegate-ok: <reason>]` in the command (a trailing "
        "`# [delegate-ok: <reason>]` comment works). An empty reason does not unblock.\n"
        "Turn the gate off: `t3 <overlay> gate delegation disable`."
    )


def investigation_shape(command: str) -> str | None:
    """The label of the unbounded shape in ``command``, or ``None`` if it routes."""
    if _PARSE_RE.search(command):
        return "a pipe into an interpreter, which reduces an output too large to read into a conclusion"
    if _SWEEP_RE.search(command) and not _BOUND_RE.search(command):
        return "an unbounded recursive search over a tree"
    return None


def _names_one_file(operand: object) -> bool:
    """Whether *operand* names ONE file rather than spelling a tree walk."""
    return isinstance(operand, str) and bool(_NAMED_FILE_RE.match(operand.rsplit("/", 1)[-1]))


def _grep_shape(tool_input: dict) -> str | None:
    """``Grep`` is the native ``rg``; its bounds are ``head_limit`` and a named file."""
    if not isinstance(tool_input.get("pattern"), str):
        return None
    head_limit = tool_input.get("head_limit")
    if isinstance(head_limit, int) and not isinstance(head_limit, bool) and head_limit > 0:
        return None
    if _names_one_file(tool_input.get("path")):
        return None
    return "an unbounded recursive search over a tree"


def _glob_shape(tool_input: dict) -> str | None:
    """``Glob`` is the native ``find``; a stem in its basename, or no ``**``, bounds the walk."""
    pattern = tool_input.get("pattern")
    if not isinstance(pattern, str) or "**" not in pattern or not _STEMLESS_GLOB_RE.match(pattern.rsplit("/", 1)[-1]):
        return None
    return "an unbounded tree walk over every file of a kind"


def native_read_shape(tool_name: str, tool_input: dict) -> str | None:
    """The label of the unbounded shape in a native read tool's arguments, or ``None``."""
    if tool_name == "Grep":
        return _grep_shape(tool_input)
    return _glob_shape(tool_input) if tool_name == "Glob" else None


def _gate_enabled() -> bool:
    """Whether the gate is on (default True); an explicit ``false`` is the kill-switch."""
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("orchestrator_delegation_gate_enabled", default=True)


def _engaged(session_id: str) -> bool:
    from hooks.scripts.hook_router import _teatree_engaged  # noqa: PLC0415 deferred back-import

    return bool(session_id) and _teatree_engaged(session_id)


def _scannable(tool_input: dict) -> str:
    """The tool call's own text, wherever a per-call token could ride in it."""
    return " ".join(value for value in tool_input.values() if isinstance(value, str))[:_TOKEN_SCAN_CHARS]


def _unattended() -> bool:
    """A CLI session nobody is watching has no orchestrator context to protect."""
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ATTENDED", "").strip() == "0"
        or os.environ.get("CLAUDE_CODE_CHILD_SESSION", "").strip() == "1"
    )


def _refusal_shape(data: dict) -> str | None:
    """The shape to refuse, or ``None``. Every check must answer positively.

    Ordered free-first: an ordinary routing call is decided by one regex pass
    and costs neither an env read, a file read, nor a DB read.
    """
    tool_input = data.get("tool_input") or {}
    tool_name = str(data.get("tool_name", ""))
    command = tool_input.get("command")
    if tool_name == "Bash":
        shape = investigation_shape(command) if isinstance(command, str) else None
    else:
        shape = native_read_shape(tool_name, tool_input)
    if shape is None or call_is_from_subagent(data) or session_lane() != LANE_INTERACTIVE_CLI or _unattended():
        return None
    if not _gate_enabled() or not _engaged(str(data.get("session_id", ""))):
        return None
    if match := _DELEGATE_OK_RE.search(_scannable(tool_input)):
        sys.stderr.write(f"NOTE: orchestrator delegation gate skipped via [delegate-ok: {match.group(1).strip()}].\n")
        return None
    return shape


def handle_block_undelegated_investigation(data: dict) -> bool:
    """Refuse an unbounded read the orchestrator should have delegated.

    The deny routes through the router's ``_fail_open_or_deny`` chokepoint, so the
    self-rescue allowlist, the master ``danger_gate_fail_open`` switch and the deny
    circuit breaker all apply on top of this gate's own token and kill-switch.
    """
    tool_name = str(data.get("tool_name", ""))
    if tool_name not in GATED_TOOLS:
        return False
    try:
        shape = _refusal_shape(data)
    except Exception:  # noqa: BLE001 — crash-proof, fail-OPEN: a broken probe never refuses
        return False
    if shape is None:
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    return _fail_open_or_deny(data, refusal(shape, tool_name), gate_id="orchestrator_delegation_gate")
