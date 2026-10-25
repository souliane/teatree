"""The recorded approval a Notion write on the MCP surface spends.

The row is ``OnBehalfApproval``, recorded by ``t3 review approve-on-behalf``. The gate is NOT
``require_on_behalf_approval``: that one lets a write through with no approval under a
permitting posture, an ``on_behalf_auto_actions`` entry or the graduated dial. Here the approval
is unconditional and is bound to a digest of the exact text the write will land.
"""

import contextlib
from collections.abc import Generator

from teatree.backends.notion.errors import NotionBlockChangedError, NotionWriteNotApprovedError
from teatree.core.gates import exact_approval


def write_target(object_path: str, *bound: str | int | None) -> str:
    return exact_approval.write_target("notion", object_path, *bound)


def approval_hint(target: str, action: str) -> dict[str, str]:
    return exact_approval.approval_hint(target, action)


@contextlib.contextmanager
def spent_approval(target: str, action: str) -> Generator[None]:
    try:
        with exact_approval.spent_approval(target, action):
            yield
    except exact_approval.ExactApprovalStaleError as exc:
        scope = target.rpartition(":")[0]
        msg = (
            f"an approval is recorded for {scope}, but for different text, or another overlay, than this call would "
            "write — the page, the arguments or the overlay changed since the dry run. Re-run it with dry_run=true "
            "and approve the new target. Nothing was written."
        )
        raise NotionBlockChangedError(msg) from exc
    except exact_approval.ExactApprovalMissingError as exc:
        msg = (
            f"no recorded approval covers this write ({action} on {target}). Show the owner the dry run, have them "
            f"record it with `{approval_hint(target, action)['record_command']}`, then call again. Nothing was written."
        )
        raise NotionWriteNotApprovedError(msg) from exc
