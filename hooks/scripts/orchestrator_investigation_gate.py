"""PreToolUse: orchestrator-investigation-boundary WARN nudge (#1442).

The heavy-Bash gate (#115, ``handle_enforce_orchestrator_boundary``) enforces the
LOAD-BEARING slice of the orchestrator-decides / loop-executes topology: it
DENIES the main agent tying its session up on a long/heavy foreground command,
and deliberately leaves Edit/Write and short Bash alone (4.x-class agents inspect
freely). The user's standing directive is broader: the t3-master orchestrator
does ONLY orchestration (spawn / collect / route / decide / advance the FSM /
communicate) and never, in the foreground, investigates, diagnoses, fixes, writes
code, or does git/CI archaeology — all of that is delegated to a sub-agent in a
worktree. That broader discipline was carried only by skill prose; this gate
makes it a structural NUDGE.

It is the broad WARN-only complement to the narrow Agent-dispatch DENY
(``_deny_foreground_agent_dispatch``). Design constraints that make it a WARN,
never a deny:

* NEVER-LOCKOUT is a hard teatree invariant. A deny here would sit on the
    orchestrator's own foreground Edit/Write/git hot path — the highest lockout
    risk in the codebase. A warn cannot wedge the session, so it is the only
    never-lockout-safe enforcement of a boundary this broad. (Pinned
    structurally: the handler has NO deny path —
    ``tests/test_orchestrator_investigation_boundary_hook.py`` asserts via AST
    that it never reaches ``emit_pretooluse_deny`` / ``_fail_open_or_deny``.)
* "Orchestration read" vs "investigation read" has no clean automatic split (a
    single ``git show`` can be either routing or archaeology). A warn that fires
    on the OBVIOUS investigation/fix signals — an Edit/Write, ``git
    show``/``blame``/``bisect``, a ``git log`` pickaxe/patch/range, a deep ``git
    diff``, ``gh``/``glab`` CI verbs, or a foreground test run — steers the
    orchestrator without ever hard-blocking a borderline call.

Scope: ONLY the live t3-master session (the session holding the ``t3-master``
``LoopLease``); a non-owner interactive session and every sub-agent pass
untouched. Off-ramps (any one suppresses the nudge): a per-call
``[orchestration-ok: <reason>]`` token (mirroring ``[fg-ok:]`` /
``[skip-skill-gate:]``) and the out-of-repo kill-switch ``[teatree]
orchestrator_investigation_gate_enabled = false``.

``call_is_from_subagent`` / ``PYTEST_VERB_FINDER`` come from the shared
``orchestration_boundary_signals`` leaf, ``teatree_bool_setting`` from
``teatree_settings``, and ``bootstrap_teatree_django`` from ``django_bootstrap``
— all dependency-free leaves, so this module imports them at top level with no
``hook_router`` cycle.
"""

import re
import sys

from hooks.scripts.django_bootstrap import bootstrap_teatree_django
from hooks.scripts.orchestration_boundary_signals import PYTEST_VERB_FINDER, call_is_from_subagent
from hooks.scripts.teatree_settings import teatree_bool_setting

# Alias both identities so the handler the router registers and a test patching a
# helper here operate on ONE module object.
sys.modules.setdefault("orchestrator_investigation_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.orchestrator_investigation_gate", sys.modules[__name__])

# Investigation/archaeology Bash shapes the t3-master should DELEGATE, not run
# inline. Read-only ORIENTATION that routes the next dispatch stays unflagged:
# ``git status``, ``git diff --stat``/``--name-only``, ``gh pr view``/``pr
# list``, ``glab mr view``/``mr list``, and ``git log --oneline -n`` are the grey
# zone left to the orchestrator. The gated set is the DEEP-DIVE archaeology and
# the CI-investigation verbs: ``git show``/``blame``/``bisect``, a ``git log``
# with a pickaxe/patch/range/follow, a ``git diff`` against a ref/range, the
# ``gh``/``glab`` ``run``/``api``/``ci``/``pipeline`` verbs, a watched ``gh pr
# checks``, and a ``gh``/``glab`` ``diff``/``logs`` subcommand — how an agent
# investigates a failure rather than routes work.
_ORCHESTRATOR_INVESTIGATION_BASH_RE = re.compile(
    r"(?:^|[;&|\n(){}])"
    r"\s*"
    r"(?:\w+=\S+\s+)*"
    r"(?:(?:command|exec|time|nice)\s+)*"
    r"(?:"
    r"git\s+(?:show|blame|bisect)\b|"
    r"git\s+log\b[^|&;]*?(?:-S|-G|-p\b|--patch|--follow)|"
    r"git\s+diff\b[^|&;]*?(?:HEAD|origin/|\.\.)|"
    r"(?:gh|glab)\s+(?:run|api|ci|pipeline)\b|"
    r"gh\s+pr\s+checks\b[^|&;]*?--watch|"
    r"(?:gh|glab)\s+\S+\s+(?:diff|logs?)\b"
    r")"
)

# ``[orchestration-ok: <non-empty-reason>]`` anywhere in the command / edit
# suppresses the nudge for the rare genuine orchestration read (e.g. a one-off
# ``git show`` to route a dispatch). An empty reason does not suppress. Mirrors
# the heavy-Bash gate's ``[fg-ok:]`` token.
_ORCHESTRATION_OK_RE = re.compile(r"\[orchestration-ok:\s*\S[^\]]*?\s*\]")

_INVESTIGATION_EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})


def _orchestrator_investigation_gate_enabled() -> bool:
    """Whether the t3-master investigation NUDGE is enabled (default True).

    Reads the ``orchestrator_investigation_gate_enabled`` DB setting via the
    shared bare-boolean reader: fails OPEN to enabled on a missing/broken row,
    and only a stored ``false`` is the one-line out-of-repo kill-switch.
    """
    return teatree_bool_setting("orchestrator_investigation_gate_enabled", default=True)


def _session_is_loop_owner(session_id: str) -> bool:
    """True iff ``session_id`` holds the live ``t3-master`` ``LoopLease``.

    The nudge is scoped to the t3-master session only — a non-owner interactive
    session inspects freely. Reuses the canonical pid-anchored liveness predicate
    (``LoopLeaseQuerySet.ownership_status``) so this never re-derives the
    "live owner" rule. Fails OPEN to NOT-owner (returns False) on a missing
    session id or any bootstrap/DB error, so a DB hiccup can never make the nudge
    fire on a non-owner — the safe direction for a steering nudge is silence.
    """
    if not session_id or not bootstrap_teatree_django():
        return False
    try:
        from teatree.core.loop_lease_manager import T3_MASTER_SLOT  # noqa: PLC0415 — deferred: cold-hook import
        from teatree.core.models import LoopLease  # noqa: PLC0415 — Django model; importable only after django.setup().

        status = LoopLease.objects.ownership_status(T3_MASTER_SLOT)
    except Exception:  # noqa: BLE001 — crash-proof: a broken resolver must never make the nudge fire on a non-owner.
        return False
    return status.is_live and status.owner_session == session_id


def _investigation_signal(data: dict) -> str | None:
    """A short human label of the investigation/fix shape in this call, or ``None``.

    Names WHAT looks like investigation/diagnosis/fix work (an Edit/Write, a deep
    ``git`` archaeology command, a ``gh``/``glab`` CI call, a foreground test run)
    so the nudge can name it. Returns ``None`` for pure-orchestration calls
    (Read/Grep, ``git status``, ``gh pr view``, the ``t3 …`` verbs) — those never
    trip the nudge.
    """
    tool_name = data.get("tool_name", "")
    if tool_name in _INVESTIGATION_EDIT_TOOLS:
        return "an Edit/Write that mutates a file"
    if tool_name != "Bash":
        return None
    command = data.get("tool_input", {}).get("command")
    if not isinstance(command, str):
        return None
    if PYTEST_VERB_FINDER.search(command):
        return "a foreground test run"
    if _ORCHESTRATOR_INVESTIGATION_BASH_RE.search(command):
        return "a deep git/CI investigation command"
    return None


def _orchestration_ok_haystack(data: dict) -> str:
    """The text scanned for an ``[orchestration-ok: <reason>]`` opt-out token.

    Bash reads ``command``; Edit/Write reads the file path and edited content
    (first 512 chars each) so the orchestrator can mark a sanctioned inline edit.
    Mirrors the skill-loading gate's per-call escape surface.
    """
    tool_input = data.get("tool_input", {})
    if data.get("tool_name") == "Bash":
        command = tool_input.get("command")
        return command if isinstance(command, str) else ""
    parts: list[str] = []
    for key in ("file_path", "new_string", "content"):
        value = tool_input.get(key)
        if isinstance(value, str):
            parts.append(value[:512])
    return " ".join(parts)


def handle_enforce_orchestrator_investigation_boundary(data: dict) -> bool:
    """NUDGE the t3-master away from foreground investigation/diagnosis/fix work.

    Structural enforcement of the broader boundary the heavy-Bash gate (#115)
    leaves to skill prose: the t3-master orchestrator does ONLY orchestration and
    delegates investigation / diagnosis / fixing / code-writing / git archaeology
    / test runs to a sub-agent in a worktree.

    This is a WARN, never a deny — it writes a one-line stderr nudge and ALWAYS
    returns ``False`` (the call proceeds). A warn is the only never-lockout-safe
    enforcement for a boundary this broad. Off-ramps that suppress the nudge: it
    fires ONLY for the live t3-master session (not sub-agents, not a non-owner
    interactive session); a per-call ``[orchestration-ok: <reason>]`` token; and
    the out-of-repo kill-switch ``[teatree]
    orchestrator_investigation_gate_enabled = false``.
    """
    if not _orchestrator_investigation_gate_enabled():
        return False
    if call_is_from_subagent(data):
        return False
    signal = _investigation_signal(data)
    if signal is None:
        return False
    if _ORCHESTRATION_OK_RE.search(_orchestration_ok_haystack(data)):
        return False
    if not _session_is_loop_owner(data.get("session_id", "")):
        return False
    sys.stderr.write(
        "[orchestration-boundary] This looks like "
        f"{signal} from the t3-master orchestrator. The orchestrator does ONLY "
        "orchestration (spawn / collect / route / decide / advance the FSM / "
        "communicate); investigation, diagnosis, fixing, code edits, git/CI "
        "archaeology, and test runs belong in a sub-agent in a worktree "
        "(background if >~30s). Dispatch one (Task/Agent, run_in_background) "
        "instead of doing the work inline. If this IS genuine orchestration, add "
        "`[orchestration-ok: <reason>]` to suppress this nudge; to disable the "
        "nudge entirely run `t3 <overlay> config_setting set "
        "orchestrator_investigation_gate_enabled false`.\n"
    )
    return False
