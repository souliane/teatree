"""DSLR snapshot retention — which snapshots are stale, and deleting them.

The retention pass the ``clean-all`` reaper runs, split from the sibling
``dslr`` module (discovery / restore / environment setup for a provisioning
import). Selection (:func:`stale_dslr_snapshots`) and deletion
(:func:`prune_dslr_snapshots`) share one implementation so ``--dry-run``
previews exactly what a live run would remove.
"""

import sys

from teatree.utils.django_db.dslr import find_dslr_cmd
from teatree.utils.run import run_allowed_to_fail


def parse_dslr_snapshots(stdout: str) -> dict[str, list[str]]:
    """Parse ``dslr list`` output, group snapshot names by tenant (suffix after date)."""
    by_tenant: dict[str, list[str]] = {}
    for line in stdout.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if not token:
            continue
        if "_" in token:
            tenant = token.split("_", maxsplit=1)[1]
            by_tenant.setdefault(tenant, []).append(token)
    for names in by_tenant.values():
        names.sort(reverse=True)
    return by_tenant


def stale_dslr_snapshots(
    *,
    keep: int = 1,
    snapshot_tool: str = "dslr",
    main_repo_path: str = "",
    in_use_tenants: set[str] | None = None,
) -> list[str]:
    """The snapshot names :func:`prune_dslr_snapshots` would delete — selection only.

    Split out so ``clean-all --dry-run`` can preview this pass with the same
    selection a live run uses, rather than describing it from the outside
    (souliane/teatree#3489).
    """
    dslr_cmd = find_dslr_cmd(snapshot_tool, main_repo_path)
    if not dslr_cmd:
        return []
    result = run_allowed_to_fail([*dslr_cmd, "list"], expected_codes=None)
    if result.returncode != 0:
        return []
    in_use = in_use_tenants or set()
    return [
        old
        for tenant, names in parse_dslr_snapshots(result.stdout).items()
        if tenant not in in_use
        for old in names[keep:]
    ]


def prune_dslr_snapshots(
    *,
    keep: int = 1,
    snapshot_tool: str = "dslr",
    main_repo_path: str = "",
    in_use_tenants: set[str] | None = None,
) -> list[str]:
    """Delete old DSLR snapshots, keeping the *keep* newest per tenant.

    Returns a list of deleted snapshot names.

    *in_use_tenants* (souliane/teatree#1306): tenants whose snapshots
    must NOT be touched because an in-flight worktree depends on them.
    A worktree mid-provision (state CREATED, DB not yet imported) needs
    the snapshot to remain restorable until provisioning completes;
    pruning unconditionally and globally destroys that with no way to
    recover short of a fresh remote dump. Pass the set of tenant strings
    (matching the DSLR snapshot suffix after the date) to skip entirely.
    """
    dslr_cmd = find_dslr_cmd(snapshot_tool, main_repo_path)
    if not dslr_cmd:
        return []
    stale = stale_dslr_snapshots(
        keep=keep, snapshot_tool=snapshot_tool, main_repo_path=main_repo_path, in_use_tenants=in_use_tenants
    )
    deleted: list[str] = []
    for old in stale:
        sys.stdout.write(f"  Pruning DSLR snapshot: {old}\n")
        run_allowed_to_fail([*dslr_cmd, "delete", "-y", old], expected_codes=None)
        deleted.append(old)
    return deleted
