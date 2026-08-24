"""PreToolUse: nudge a hand-rolled landed-ness probe toward the canonical answer (#4070).

An orchestrator ran ``git cherry origin/main HEAD`` across ~18 worktrees, concluded
four branches held unmerged work, escalated three to the owner as false completions
and dispatched a shipper to push them. Three were already on main via squash-merge —
which rewrites the branch's shas, so every per-commit / ancestor primitive misreads it.
:mod:`teatree.core.worktree.branch_classification` had answered this correctly for
months and was simply not reached for; the failure was an agent TYPING bash, so the
enforcement lives at the Bash chokepoint.

WARN-only, never a deny. "Is this branch landed?" and "which of my commits are already
upstream while I rebase?" are the same command, and no automatic split between them is
reliable — the same constraint that made the #1442 investigation nudge a warn. That
ambiguity is precisely why a deny would be wrong: it would sit on the git hot path and
refuse legitimate work. The advisory costs a mistaken caller one line; the missing
advisory cost three false escalations. (Pinned structurally: this module never reaches
``emit_pretooluse_deny`` / ``_fail_open_or_deny`` —
``tests/test_merged_detection_probe_gate.py`` asserts it by AST.)

The canonical implementation needs no carve-out: ``branch_classification`` shells out
through Python ``subprocess`` inside the worker, and a PreToolUse hook sees only the
agent's own Bash tool calls, so the gate is structurally incapable of firing on its own
detector or on the pytest run that exercises it.

Never-lockout trio: the per-call ``[merge-detect-ok: <reason>]`` token, the
``merged_detection_gate_enabled = false`` kill-switch
(``t3 <overlay> gate merged-detect disable``), and a silent fail-open on any resolver
error.

Cold-import safe: the live hook is a bare ``python3`` subprocess with no guarantee
``teatree`` is importable, so the module top imports only stdlib and dependency-free
hook siblings; the detection leaf is imported lazily inside the ``src/`` bootstrap.
"""

import re
import sys
from typing import Final

from hooks.scripts.managed_repo import teatree_src_on_path as _teatree_src_on_path
from hooks.scripts.teatree_settings import teatree_bool_setting

# Alias both identities so the handler the router registers and a test patching a
# helper here operate on ONE module object.
sys.modules.setdefault("merged_detection_probe_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.merged_detection_probe_gate", sys.modules[__name__])

_MERGE_DETECT_OK_RE: Final[re.Pattern[str]] = re.compile(r"\[merge-detect-ok:\s*\S[^\]]*?\s*\]")
_TOKEN_SCAN_LIMIT: Final[int] = 512

_ADVISORY: Final[str] = (
    "[merged-detection] This looks like a hand-rolled `{shape}` — a squash-merge rewrites the "
    "branch's shas, so a per-commit / ancestor / `--not` test reads landed work as unmerged. "
    "That misread escalated three already-merged branches to the owner as false completions. "
    "The canonical answer is the three-layer content classifier (cherry-zero / synthetic-squash "
    "/ branch-merged): run `t3 <overlay> workspace branch-verdict <branch>` for one branch's "
    "verdict, or `t3 <overlay> workspace emit` for the sweep. If this probe is asking something "
    "else, add `[merge-detect-ok: <reason>]` to the command; to silence the nudge entirely run "
    "`t3 <overlay> gate merged-detect disable`.\n"
)


def handle_warn_merged_detection_probe(data: dict) -> bool:
    """NUDGE a hand-rolled landed-ness probe toward ``workspace branch-verdict``.

    Writes one stderr advisory and ALWAYS returns ``False`` — the call proceeds, whatever
    the probe said. There is no deny path in this module at all.
    """
    if (shape := _shape_to_nudge(data)) is not None:
        sys.stderr.write(_ADVISORY.format(shape=shape))
    return False


def _shape_to_nudge(data: dict) -> str | None:
    """The landed-ness probe this call runs and deserves a nudge for, else ``None``.

    Silent for a non-Bash tool, a probe against a non-default target (a coder's own
    upstream, ``repro``'s two-sha ancestry proof), a ``[merge-detect-ok: <reason>]``
    token, a disabled kill-switch, and any internal error.
    """
    if data.get("tool_name") != "Bash":
        return None
    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not command:
        return None
    try:
        if not teatree_bool_setting("merged_detection_gate_enabled", default=True):
            return None
        if _MERGE_DETECT_OK_RE.search(command[:_TOKEN_SCAN_LIMIT]):
            return None
        with _teatree_src_on_path():
            from teatree.hooks import merged_detection_probe  # noqa: PLC0415 — deferred: cold-hook import

            return merged_detection_probe.merged_detection_shape(command)
    except Exception:  # noqa: BLE001 — crash-proof: a broken resolver must never speak up, nor break the call.
        return None
