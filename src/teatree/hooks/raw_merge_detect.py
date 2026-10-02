"""Action-aware detection of a raw forge-merge invocation — the out-of-band-merge gate (#2387).

The PreToolUse gate (BLUEPRINT §17.1 invariant 8) blocks a raw ``gh pr merge`` /
``glab mr merge`` on a teatree-managed repo because it bypasses the FSM keystone
merge. The original matcher searched for the subcommand phrase as a SUBSTRING
anywhere in the Bash command text, so a command that merely *documents* the merge
command — a ``cat >> note.md <<EOF … gh pr merge … EOF`` heredoc, an
``echo "run gh pr merge"`` string, or a ``# gh pr merge`` comment — was wrongly
blocked (same content-not-action over-block class as #1415).

This detection is a STRICT tightening of that substring matcher, not a
re-scoping. It removes ONLY the provably-non-invocation false positives — a
heredoc body, a ``#`` comment, and a quoted-string operand — and otherwise errs
toward BLOCK: any plausible invocation of the merge subcommand fires.

A merge is an INVOCATION when the merge subcommand is the executed program of a
command segment. That traversal — the env-assignment / wrapper / compound-keyword
strip, the path-qualified basename match, and the recursion into command
substitutions — is the shared :mod:`teatree.hooks.forge_subcommand` leaf, so the
CREATE gate (:mod:`teatree.hooks.raw_create_detect`) recognises exactly the same
invocation forms and the two can never drift.
"""

import re

from teatree.hooks.forge_subcommand import GLAB_GH_API_RE as _GLAB_GH_API_RE
from teatree.hooks.forge_subcommand import effective_method_is_write as _effective_method_is_write
from teatree.hooks.forge_subcommand import invokes_forge_subcommand

# The REST merge endpoint: ``(merge_requests|pulls)/<iid>/merge`` (GitLab + GitHub
# shapes). The iid is ``[^/\s]+`` -- ANY non-slash, non-space token -- not just a
# numeric ``\d+``: a shell-variable or brace-templated iid
# (``pulls/$PR/merge``, ``pulls/{id}/merge``, ``merge_requests/$IID/merge``)
# resolves to a real merge at run time, so the numeric-only pattern let it evade
# the out-of-band-merge hard-deny (#F7.8). Errs toward BLOCK, consistent with the
# fail-closed doctrine; a GET to the same endpoint is still allowed via the
# effective-method check.
_MERGE_ENDPOINT_RE = re.compile(r"(?:merge_requests|pulls)/[^/\s]+/merge\b")
# GitHub GraphQL merge-effecting mutations (each merges a PR / branch out of band).
_GRAPHQL_MERGE_MUTATION_RE = re.compile(r"(?:mergePullRequest|enablePullRequestAutoMerge|mergeBranch)\s*\(")

# The two forge programs and the merge subcommand words that follow.
_MERGE_SUBWORDS: dict[str, tuple[str, ...]] = {"gh": ("pr", "merge"), "glab": ("mr", "merge")}

_RAW_MERGE_DENY_REASON = (
    "BLOCKED: raw `gh pr merge` / `glab mr merge` on a teatree-managed repo — "
    "an out-of-band merge bypasses the FSM coherence mechanism (ledger update, "
    "MergeClear validation, SHA-binding, privacy/AI-signature scan, mark_merged). "
    "Use the sanctioned keystone transition `t3 <overlay> ticket merge <clear_id>` "
    "(BLUEPRINT §17.1 invariant 8 / §17.4). kill-switch: `t3 <overlay> gate raw-merge disable`."
)


def is_raw_merge_api_write(command: str) -> bool:
    """Whether *command* is a raw forge REST WRITE to a ``.../<n>/merge`` endpoint.

    True only when the command targets a ``.../pulls/<n>/merge`` or
    ``.../merge_requests/<n>/merge`` endpoint AND its effective HTTP method is not
    GET (a GET reads merge status and must NOT be denied).
    """
    if not command or not _GLAB_GH_API_RE.search(command):
        return False
    if not _MERGE_ENDPOINT_RE.search(command):
        return False
    return _effective_method_is_write(command)


def invokes_graphql_merge_mutation(command: str) -> bool:
    """Whether *command* is a ``gh``/``glab api`` GraphQL merge-effecting mutation.

    A ``mergePullRequest`` / ``enablePullRequestAutoMerge`` / ``mergeBranch`` call
    has an unresolvable node-id target, so any occurrence in a forge-``api`` command
    is treated as a merge (fail-closed). A query moved out of argv (``-F query=@file``
    / ``--input``) is an accepted residual, matching the router gate.
    """
    if not command or not _GLAB_GH_API_RE.search(command):
        return False
    return bool(_GRAPHQL_MERGE_MUTATION_RE.search(command))


def invokes_raw_merge_subcommand(command: str) -> bool:
    """Whether *command* INVOKES ``gh pr merge`` / ``glab mr merge`` as an executed program.

    Errs toward BLOCK: fires on any plausible invocation (env-prefixed,
    wrapper-prefixed, path-qualified, grouped/compound, or inside a command
    substitution). Only a heredoc body, a ``#`` comment, and a quoted-string
    operand — provably-non-invocation text — pass through.
    """
    return invokes_forge_subcommand(command, _MERGE_SUBWORDS)


def raw_merge_deny_reason(command: str) -> str | None:
    """Return the raw-merge deny reason for *command*, or ``None`` when it is allowed.

    Fires on any of the three out-of-band merge vectors — the literal subcommand
    (``gh pr merge`` / ``glab mr merge``, action-aware), the REST-API merge write, or
    a GraphQL merge mutation. This is the PURE detector shared by
    :mod:`teatree.hooks.hard_deny_registry` (Lane B) and delegated to by the router's
    cwd-aware merge gate; the unmanaged-repo carve-out (#126) is hook_router context
    layered ON TOP of this detector, never part of it — Lane B is always jailed to a
    managed worktree, so it denies every raw merge unconditionally.
    """
    if not command:
        return None
    if (
        invokes_raw_merge_subcommand(command)
        or is_raw_merge_api_write(command)
        or invokes_graphql_merge_mutation(command)
    ):
        return _RAW_MERGE_DENY_REASON
    return None


__all__ = [
    "invokes_graphql_merge_mutation",
    "invokes_raw_merge_subcommand",
    "is_raw_merge_api_write",
    "raw_merge_deny_reason",
]
