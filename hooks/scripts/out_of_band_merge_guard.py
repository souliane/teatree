"""Deny a raw ``gh pr merge`` / ``glab mr merge`` aimed at a teatree-managed repo (#126).

``gh pr merge`` / ``glab mr merge`` bypass the FSM coherence mechanism (ledger update,
MergeClear validation, SHA-binding, privacy/AI-signature scan, mark_merged), so a
TEATREE-MANAGED repo must use the keystone transition ``t3 <overlay> ticket merge
<clear_id>`` (BLUEPRINT §17.1 invariant 8 / §17.4). The previous static-regex block
hard-denied EVERY repo — a lightweight repo with no ticket/overlay FSM had no merge path
at all, a permanent lockout (#126).

This cwd-aware gate carves out the unmanaged case: a merge is ALLOWED only when the cwd
repo is confidently NOT teatree-managed (no overlay claims it). The gate stays STRICT for
managed repos AND fail-safe on uncertainty: when the cwd or its slug cannot be resolved,
the repo is treated as managed and the merge is BLOCKED — detection failure never weakens
the gate.

The router re-exports :func:`handle_block_out_of_band_merge` into ``_HANDLERS``. The
router internals the handler shares (the fail-open chokepoint, the cwd resolver and the
cold settings reader) are back-imported at call time, so a test patching them on the
router still steers this gate.
"""

import re
import sys

from hooks.scripts.forge_api_detect import _GLAB_GH_API_RE, invokes_raw_merge_subcommand, is_raw_merge_api_write
from hooks.scripts.managed_repo import cwd_teatree_managed_state, overlay_managed_repo_signals
from hooks.scripts.mr_cli_fields import merge_target_managed_state

sys.modules.setdefault("out_of_band_merge_guard", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.out_of_band_merge_guard", sys.modules[__name__])

_OUT_OF_BAND_MERGE_RE = re.compile(r"\b(?:gh\s+pr\s+merge|glab\s+mr\s+merge)\b")
# GitHub GraphQL merge-effecting mutations (all merge the PR / a branch out of band):
# mergePullRequest, enablePullRequestAutoMerge (native-rules merge), mergeBranch.
_GRAPHQL_MERGE_MUTATION_RE = re.compile(r"(?:mergePullRequest|enablePullRequestAutoMerge|mergeBranch)\s*\(")
_OUT_OF_BAND_MERGE_REASON = (
    "BLOCKED: raw `gh pr merge` / `glab mr merge` on a teatree-managed repo — "
    "an out-of-band merge bypasses the FSM coherence mechanism (ledger update, "
    "MergeClear validation, SHA-binding, privacy/AI-signature scan, mark_merged). "
    "Use the sanctioned keystone transition `t3 <overlay> ticket merge <clear_id>` "
    "(BLUEPRINT §17.1 invariant 8 / §17.4). If this repo is genuinely not "
    "teatree-managed, name the target explicitly with `--repo <owner>/<repo>` (or "
    "a full forge URL) so the gate classifies it from the command itself and never "
    "consults the cwd. kill-switch: `t3 <overlay> gate raw-merge disable`."
)


def handle_block_out_of_band_merge(data: dict) -> bool:
    """Block a raw/REST-API/graphql merge form aimed at a teatree-managed repo.

    Three bypass vectors. The literal subcommand (``gh pr merge`` / ``glab mr
    merge``) is matched action-aware by :func:`invokes_raw_merge_subcommand`
    (a real invocation, not a heredoc/echo/comment — #2387). The REST-API form
    (``gh api .../pulls/<n>/merge -X PUT``) is matched by
    :func:`is_raw_merge_api_write` (a GET read is NOT denied). A graphql merge
    mutation (mergePullRequest / enablePullRequestAutoMerge / mergeBranch) has an
    unresolvable node-id target, so it is blocked (fail-closed).

    Otherwise classification keys on the merge TARGET via a tri-state
    (:func:`merge_target_managed_state`): a managed target is BLOCKED regardless of
    cwd; a confidently-unmanaged target is ALLOWED on its own evidence; only when NO
    target parses does the cwd-keyed fallback (#126) run (#3343).
    """
    from hooks.scripts.hook_router import (  # noqa: PLC0415 — deferred back-import: patches on the router steer it
        _fail_open_or_deny,
        _resolve_cwd_repo,
        _teatree_bool_setting,
    )

    if data.get("tool_name") != "Bash":
        return False
    command = data.get("tool_input", {}).get("command", "")
    # No command, or the kill-switch (`t3 <overlay> gate raw-merge disable`) is off → nothing to gate.
    if not command or not _teatree_bool_setting("out_of_band_merge_gate_enabled", default=True):
        return False
    # Matches the mutation name in argv only: a query loaded from a file or stdin
    # (`gh api graphql -F query=@file` / `--input`) moves the text out of argv and
    # is NOT inspected — an accepted residual, not a silent miss.
    if _GLAB_GH_API_RE.search(command) and _GRAPHQL_MERGE_MUTATION_RE.search(command):
        return _fail_open_or_deny(data, _OUT_OF_BAND_MERGE_REASON, gate_id="out_of_band_merge")
    if not invokes_raw_merge_subcommand(command) and not is_raw_merge_api_write(command):
        return False
    # Tri-state target classification (#3343): a managed target → BLOCK regardless
    # of cwd; a confidently-UNMANAGED target → ALLOW on its own evidence, never
    # keyed off cwd (the bug: a non-git cwd forced a deny on a classifiable target).
    # Only a NON-resolvable target (None) consults the cwd fallback (#126), allowing
    # a confidently-unmanaged cwd (a non-classifiable cwd fails safe to BLOCK).
    target_state = merge_target_managed_state(command, overlay_managed_repo_signals()[0])
    if target_state is None:
        cwd = _resolve_cwd_repo(data)
        target_state = not (cwd is not None and cwd_teatree_managed_state(cwd) is False)
    if target_state is False:
        return False
    return _fail_open_or_deny(data, _OUT_OF_BAND_MERGE_REASON, gate_id="out_of_band_merge")
