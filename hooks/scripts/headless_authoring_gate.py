"""PreToolUse: refuse INTERACTIVE authoring of teatree while the headless posture is set (#3883).

``agent_runtime = headless`` declares that implementation work runs through the factory.
Nothing enforced it, so an interactive session could hand-write ``src/``, dispatch ten
``t3:coder`` agents, and commit — each step individually reasonable, none of it refused.
The instruction half of the control (#3869) is prose an agent can reason around; this is
the deterministic backstop underneath it.

The line: **the main session monitors and dogfoods; the factory implements.** Reading,
searching, diagnosing, reviewing, merging, answering questions, filing issues, and every
host operation the factory cannot do (disk reclaim, DB prune, killing a stuck process)
stay ALLOWED — that is the session's actual job, and dogfooding requires running ``t3``
commands that mutate host state. What is refused is authoring the repo's own source, judged
for a ``Bash`` write by the repo the command LANDS in (:func:`_bash_repo_probe`) rather than by
wherever the session happens to sit.

WHO IS ACTING, NOT WHAT IS TOUCHED
----------------------------------
The factory's own workers run through the Agent SDK with this SAME hook set. A gate keyed
on the path would refuse the agents meant to do the implementing, and the failure would
present as "every headless task refuses" with no obvious cause. So the discriminator is
the LANE (``session_lane.session_lane``, shared with the engagement seam). Only a
positively-identified interactive CLI session is ever refused.

FAILS OPEN, INVERTING THE HOUSE RULE
------------------------------------
Every other destructive gate here fails CLOSED. This one fails OPEN, deliberately: a wrong
refusal halts the entire factory and every SDK consumer, while a wrong allow costs one
hand-written edit a human will notice. The blast radii are not comparable. An "unknown"
verdict — an unreadable lane signal, an unreadable posture, any internal error — is NOT
evidence of an interactive session, and ALLOWS.

Cold-import safe: stdlib-only at module top; the router helpers and the ``teatree`` config
read are imported lazily inside the functions.
"""

import os
import re
import sys
import time
from pathlib import Path

from hooks.scripts.managed_repo import repo_root_is_teatree_managed, resolve_branch_and_root, teatree_src_on_path
from hooks.scripts.mr_cli_fields import strip_quoted_and_heredoc
from hooks.scripts.session_lane import LANE_INTERACTIVE_CLI, LANE_SDK, LANE_UNKNOWN, session_lane

# Alias both identities so the handler the router registers and a test patching a helper
# here operate on the SAME module object — the pattern every sibling uses.
sys.modules.setdefault("headless_authoring_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.headless_authoring_gate", sys.modules[__name__])

#: The audited, single-use override. Modelled on the sibling ``[main-clone-ok:]`` /
#: ``[skill-load-ok:]`` tokens, and RECORDED (unlike them): a genuine emergency — the factory
#: itself is down and a fix is needed to restore it — must not be blocked by the thing it is
#: trying to repair, but it must leave a trace. An empty reason does not unblock.
_OVERRIDE_RE = re.compile(r"\[headless-authoring-ok:\s*(\S[^\]]*?)\s*\]")

#: How much of a call's text the override scanner reads. A ``git commit`` puts its message
#: — the documented place to carry the token in an emergency — thousands of characters into
#: the command, so a window sized for a flag line makes the escape unreachable for exactly
#: the calls most likely to need it. Bounded rather than unlimited because a ``Write``
#: ``content`` field has no size ceiling and the hook has a 30s one.
_OVERRIDE_SCAN_MAX_CHARS = 64 * 1024

#: The directories that ARE teatree's authored source. A path outside them (a scratch file,
#: a log, a note) is not authoring and is never refused.
_AUTHORED_DIRS: tuple[str, ...] = ("src", "tests", "skills", "hooks", "docs", "agents", "evals", "scripts")

#: Sub-agent types that IMPLEMENT. Deliberately not every agent: the read-only and
#: coordinating ones (``Explore``, ``t3:reviewer``, ``t3:followup``, ``t3:triage-assessor``)
#: are the main session's own role and must stay dispatchable.
_IMPLEMENTATION_SUBAGENTS: frozenset[str] = frozenset(
    {"t3:coder", "t3:debugger", "t3:tester", "t3:e2e", "t3:shipper", "t3:planner"}
)

_FILE_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "NotebookEdit"})

#: A ``Bash`` command that WRITES history into the repo. ``git commit`` / ``git push`` only —
#: never a read-only git, never a ``t3`` command (the dogfooding surface). Matched against the
#: quote/heredoc-stripped skeleton, as in every sibling gate: the same words inside a ``-m``
#: message, a ``--grep`` pattern or a heredoc body are text, not an invocation, and the
#: separator anchor is satisfied by any ``&&`` that happens to precede them there. The
#: residual — a real push fed THROUGH a stripped span (``bash -c "git push"``) — is the
#: false-negative every sibling accepts, and the right one for a gate that fails OPEN.
_AUTHORING_BASH_RE = re.compile(r"(?:^|[;&|]\s*)(?:sudo\s+)?git\s+(?:-C\s+\S+\s+)?(?:commit|push)\b")

_REFUSAL = (
    "HEADLESS POSTURE: this session is teatree-engaged and `agent_runtime = headless`, which "
    "means implementation of teatree runs through the factory, not by hand here. This session's "
    "job is to monitor and dogfood — read, search, diagnose, review, merge, answer questions, "
    "run `t3`, and FILE what it finds.\n"
    "Refused: an edit, a `git commit`, or a `git push` — push included — whose resolved target "
    "is a teatree-managed repo. A leading `cd <dir> &&` or a `git -C <dir>` decides which repo "
    "that is, so only the repo you are actually writing to is gated.\n"
    "Do this instead: find or file the issue on THAT repo (`gh issue create --repo <owner>/<repo> "
    "...`), then let intake pick it up; check progress with `t3 <overlay> followup sync`.\n"
    "Already-started work is exempt — an edit, a commit OR a push inside a live t3 worktree for "
    "the ticket is allowed.\n"
    "For a genuine emergency (the factory itself is down), put `[headless-authoring-ok: <reason>]` "
    "INSIDE the call's own text: the `command` string for Bash (a trailing "
    "`# [headless-authoring-ok: <reason>]` comment works, and the first 64 KiB is read, so a "
    "commit message carries it), or `new_string`/`content` for a file edit. The `description` "
    "field is NOT scanned. It unblocks exactly this one action and is recorded.\n"
    "To turn the gate off entirely: "
    "`t3 <overlay> config_setting set headless_authoring_gate_enabled false`."
)


def _gate_enabled() -> bool:
    """Whether the gate is on (default True). ``headless_authoring_gate_enabled = false`` disables it."""
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("headless_authoring_gate_enabled", default=True)


def _posture_is_headless() -> bool | None:
    """Whether ``agent_runtime`` resolves to ``headless``; ``None`` when it cannot be read.

    ``None`` is the fail-OPEN answer and is returned for every unreadable shape — teatree not
    importable from the hook interpreter, an unreachable DB, an unrecognised value. A posture
    teatree cannot read is not a posture it may enforce.
    """
    try:
        with teatree_src_on_path():
            from teatree.config.cold_reader import overlay_then_global  # noqa: PLC0415 — deferred: cold-hook import

            # Overlay scope first, then global — the cold twin of the resolver's own two-tier
            # layering, so a per-overlay runtime beats the workspace-wide one exactly as it
            # does in ``get_effective_settings``. An unresolvable overlay reads global only.
            value = overlay_then_global("agent_runtime", os.environ.get("T3_OVERLAY_NAME", "").strip())
    except Exception:  # noqa: BLE001 — crash-proof: an unreadable posture ALLOWS, never refuses
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower() == "headless"


def _targets_teatree_repo(file_path: str) -> bool:
    """Whether *file_path* is authored source inside a teatree-managed repo.

    Two conditions, both required: the path sits under one of :data:`_AUTHORED_DIRS`
    relative to its repo root, AND that repo is teatree-managed. Anything unresolvable is
    ``False`` — the fail-open direction.
    """
    if not file_path:
        return False
    try:
        resolved = resolve_branch_and_root(str(Path(file_path).parent))
        if resolved is None:
            return False
        _branch, root = resolved
        if not repo_root_is_teatree_managed(root):
            return False
        relative = Path(file_path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False
    return bool(relative.parts) and relative.parts[0] in _AUTHORED_DIRS


def _path_is_in_live_worktree(file_path: str) -> bool:
    """Whether *file_path* lives in a LINKED git worktree rather than a primary clone.

    This is the in-flight carve-out. A t3-managed worktree exists only because
    ``t3 <overlay> workspace ticket`` created one for a ticket, so its presence IS "there is
    already a live checkout for this work". Handing that back to the factory would mean
    reconciling state it is cheaper to finish where it started; only NEW work — authored in
    the primary clone, with no checkout behind it — is refused.

    A linked worktree has a ``.git`` FILE; a primary clone has a ``.git`` DIRECTORY.
    Unresolvable is ``True`` (allow), the fail-open direction.
    """
    if not file_path:
        return True
    try:
        resolved = resolve_branch_and_root(str(Path(file_path).parent))
        if resolved is None:
            return True
        _branch, root = resolved
        return (Path(root) / ".git").is_file()
    except (OSError, ValueError):
        return True


def _bash_repo_probe(command: str, cwd: str) -> str:
    """The stand-in path a ``Bash`` call is judged by, since it names no file.

    Built from the dir the git command's write LANDS in (``resolve_commit_dir`` — a leading
    ``cd <dir> &&`` and ``git -C <dir>`` both move it), never from the ambient hook cwd on its
    own. The cwd is where the SESSION sits, and a session sits in one repo while committing to
    another all day; keying on it refused every commit and push a session made, to any
    repository, purely because the session's own directory was managed.

    Both repo questions the file branch asks of a real ``file_path`` — is this teatree's
    authored source (:func:`_targets_teatree_repo`), and is it already a live checkout
    (:func:`_path_is_in_live_worktree`) — are asked of this ONE probe, so the two can never
    resolve to different repos for the same command.

    ``""`` when the target cannot be resolved statically (an unexpanded ``$VAR``, a ``cd``
    onto no real directory): an unknown target is not evidence of authoring, and this gate
    allows on unknown.
    """
    with teatree_src_on_path():
        from teatree.hooks._commit_repo_dir import resolve_commit_dir  # noqa: PLC0415, PLC2701 — cold-hook import

        landing = resolve_commit_dir(command, Path(cwd) if cwd else None)
    return str(landing / "src" / "_") if isinstance(landing, Path) else ""


def _call_text(data: dict) -> str:
    """The tool call's own text, where a per-call override token would appear."""
    tool_input = data.get("tool_input", {}) or {}
    parts = [
        str(tool_input.get(field, "") or "")
        for field in ("command", "new_string", "content", "file_path", "prompt", "new_source")
    ]
    return " ".join(parts)[:_OVERRIDE_SCAN_MAX_CHARS]


def _consume_override(data: dict, session_id: str) -> str:
    """Return the override reason if this call carries one, recording it; else ``""``.

    Recorded per session in ``<session>.authoring-overrides``, one line per use, so a bypass
    leaves a trace rather than being an invisible escape. A failure to WRITE the record does
    not withhold the override — the operator is mid-emergency, and refusing them because an
    audit line could not be appended is the lockout this whole gate must not become.
    """
    match = _OVERRIDE_RE.search(_call_text(data))
    if match is None:
        return ""
    reason = match.group(1).strip()
    if not reason:
        return ""
    try:
        from hooks.scripts.hook_router import _append_line, _ensure_state_dir, _state_file  # noqa: PLC0415 back-import

        _ensure_state_dir()
        tool = str(data.get("tool_name", "?"))
        _append_line(_state_file(session_id, "authoring-overrides"), f"{int(time.time())}\t{tool}\t{reason}")
    except Exception:  # noqa: BLE001 — the audit line is best-effort; it never withholds the override
        sys.stderr.write(f"WARNING: headless-authoring override used but NOT recorded: {reason}\n")
    sys.stderr.write(f"NOTE: headless-authoring gate overridden for one call: {reason}\n")
    return reason


def _is_authoring_call(data: dict) -> bool:
    """Whether this call AUTHORS teatree — the only shape the gate refuses."""
    tool = str(data.get("tool_name", ""))
    tool_input = data.get("tool_input", {}) or {}
    if tool in _FILE_TOOLS:
        path = str(tool_input.get("file_path", "") or tool_input.get("notebook_path", "") or "")
        return _targets_teatree_repo(path) and not _path_is_in_live_worktree(path)
    if tool == "Agent":
        return str(tool_input.get("subagent_type", "")).strip() in _IMPLEMENTATION_SUBAGENTS
    if tool == "Bash":
        command = str(tool_input.get("command", "") or "")
        if not _AUTHORING_BASH_RE.search(strip_quoted_and_heredoc(command)):
            return False
        # Same two questions, same order, same carve-out as the file branch above. Without
        # the second, an agent could author a change inside a live worktree and then not
        # commit it — the edit allowed, the commit refused, for one piece of work.
        probe = _bash_repo_probe(command, str(data.get("cwd", "") or Path.cwd()))
        return _targets_teatree_repo(probe) and not _path_is_in_live_worktree(probe)
    return False


def _refusal_applies(data: dict, session_id: str) -> bool:
    """Whether every POSITIVE precondition for a refusal holds.

    Split from the handler so each condition reads as one line and the fail-open default
    is stated once: any answer that is not a definite yes leaves this ``False``.
    """
    if session_lane() != LANE_INTERACTIVE_CLI or not _gate_enabled():
        return False
    from hooks.scripts.hook_router import _teatree_engaged  # noqa: PLC0415 deferred back-import

    if not _teatree_engaged(session_id) or _posture_is_headless() is not True:
        return False
    if not _is_authoring_call(data):
        return False
    return not _consume_override(data, session_id)


def handle_block_interactive_authoring(data: dict) -> bool:
    """Refuse interactive authoring of teatree under the headless posture (#3883).

    Every condition must hold POSITIVELY before anything is refused (:func:`_refusal_applies`):
    the gate is enabled, the lane is a positively-identified interactive CLI session, teatree
    is engaged, the posture reads ``headless``, the call authors teatree's own source, and no
    audited override is present. Any unreadable answer allows.

    The deny routes through the router's ``_fail_open_or_deny`` chokepoint, so the self-rescue
    allowlist, the master ``danger_gate_fail_open`` switch, and the deny circuit breaker all
    apply on top of the gate's own escapes.
    """
    session_id = str(data.get("session_id", ""))
    if not session_id:
        return False
    try:
        applies = _refusal_applies(data, session_id)
    except Exception:  # noqa: BLE001 — crash-proof, fail-OPEN: a broken probe never refuses
        return False
    if not applies:
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    return _fail_open_or_deny(data, _REFUSAL, gate_id="headless_authoring")


__all__ = [
    "LANE_INTERACTIVE_CLI",
    "LANE_SDK",
    "LANE_UNKNOWN",
    "handle_block_interactive_authoring",
    "session_lane",
]
