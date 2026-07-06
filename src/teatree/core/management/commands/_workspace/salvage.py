"""Engines for the ``workspace emit`` + ``workspace salvage`` commands (#2763).

The CLI methods on :class:`Command` stay thin wrappers (Django: views coordinate
only); the structured-EMIT rendering and the capture-verify-delete salvage
orchestration live here.
"""

import json
from pathlib import Path

from teatree.core.cleanup.cleanup_salvage import SalvageRequest, default_salvage_hooks, salvage_item
from teatree.core.worktree.worktree_done import collect_emit_records
from teatree.utils import git


def emit_records_json(workspace: Path) -> str:
    """Render the JSON array of NOT-auto-deleted records the judgment skill consumes."""
    records = [record.to_dict() for record in collect_emit_records(workspace)]
    return json.dumps(records, indent=2)


def run_salvage(source_ref: str, *, salvage_branch: str, target: str, allow_banned: bool) -> str:
    """Capture ``source_ref``'s unique content to a PR, verify, then delete the branch.

    Operates on the current repo (cwd). Fail-safe: the source branch is deleted
    ONLY after the forge confirms the PR (see :func:`salvage_item`). Returns the
    one-line outcome the CLI prints.
    """
    repo = git.run(repo=str(Path.cwd()), args=["rev-parse", "--show-toplevel"]) or str(Path.cwd())
    branch = salvage_branch or f"salvage/{source_ref}"
    hooks = default_salvage_hooks(
        source_branch=source_ref,
        delete=lambda: [] if git.branch_delete(repo, source_ref) else [f"git branch -D {source_ref} failed"],
    )
    result = salvage_item(
        SalvageRequest(
            repo=repo,
            source_ref=source_ref,
            salvage_branch=branch,
            target=target,
            require_banned_clean=not allow_banned,
        ),
        hooks,
    )
    return (
        f"salvaged={result.salvaged} deleted={result.deleted} "
        f"branch={result.salvage_branch} pr={result.pr_url or '-'}"
        + (f" errors={'; '.join(result.errors)}" if result.errors else "")
    )
