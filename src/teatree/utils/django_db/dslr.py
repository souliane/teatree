"""DSLR snapshot helpers for Django database provisioning.

Handles discovery, restore, pruning, and environment setup for DSLR
(Django Lightweight Snapshot Restore) snapshots used by the DB import
engine in the sibling ``importer`` module.
"""

import os
import re
import shutil
import sys
from datetime import UTC, datetime

from teatree.utils import bad_artifacts
from teatree.utils.django_db.helpers import _local_db_url
from teatree.utils.run import run_allowed_to_fail


def find_dslr_cmd(tool_name: str, _main_repo_path: str = "") -> list[str]:
    """Return a command prefix for invoking dslr.

    Uses ``uv run`` from the **host project** (where dslr + psycopg live as
    hard dependencies), not from the target repo.  The *main_repo_path* arg
    is accepted for backward compatibility but ignored — dslr must be in the
    teatree host project's venv.

    Honour ``DSLR_CMD`` env var as an explicit override.
    """
    dslr = os.environ.get("DSLR_CMD", "")
    if dslr and shutil.which(dslr):
        return [dslr]
    if shutil.which("uv"):
        return ["uv", "run", tool_name]
    return []


def dslr_env(ref_db: str) -> dict[str, str]:
    url = _local_db_url(ref_db)
    return {**os.environ, "DATABASE_URL": url, "DSLR_DB_URL": url}


def dslr_snap_name(ref_db: str) -> str:
    today = datetime.now(tz=UTC).strftime("%Y%m%d")
    return f"{today}_{ref_db}"


def dslr_artifact_key(snap_name: str) -> str:
    return f"dslr:{snap_name}"


def find_dslr_snapshots(dslr_cmd: list[str], env: dict[str, str], ref_db: str) -> list[str]:
    """Return matching DSLR snapshots sorted newest-first, excluding bad artifacts."""
    suffix = f"_{ref_db}"
    result = run_allowed_to_fail([*dslr_cmd, "list"], env=env, expected_codes=None)
    if result.returncode != 0:
        return []
    names: list[str] = []
    for line in result.stdout.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if token.endswith(suffix) and not bad_artifacts.is_bad(dslr_artifact_key(token)):
            names.append(token)
    names.sort(reverse=True)
    return names


def is_env_error(stderr: str) -> bool:
    """Return True if the error is environmental (connection, auth), not data corruption."""
    env_patterns = [
        "connection refused",
        "could not connect",
        "password authentication failed",
        "SSL",
        "ssl",
        "no pg_hba.conf entry",
        "timeout expired",
        "server closed the connection",
    ]
    lower = stderr.lower()
    return any(p.lower() in lower for p in env_patterns)


def restore_ref_from_dslr(dslr_cmd: list[str], env: dict[str, str], snap_name: str) -> tuple[bool, bool, str]:
    """Restore a DSLR snapshot. Returns (success, is_env_error, stderr)."""
    result = run_allowed_to_fail([*dslr_cmd, "restore", snap_name], env=env, expected_codes=None)
    if result.returncode == 0:
        return True, False, ""
    return False, is_env_error(result.stderr), result.stderr.strip()


def extract_failing_migration(stdout: str) -> str | None:
    match = re.search(r"Applying (\w+\.\w+)\.\.\.", stdout)
    return match.group(1) if match else None


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
    result = run_allowed_to_fail([*dslr_cmd, "list"], expected_codes=None)
    if result.returncode != 0:
        return []
    in_use = in_use_tenants or set()
    by_tenant = parse_dslr_snapshots(result.stdout)
    deleted: list[str] = []
    for tenant, names in by_tenant.items():
        if tenant in in_use:
            sys.stdout.write(f"  Skipping DSLR prune for in-use tenant: {tenant}\n")
            continue
        for old in names[keep:]:
            sys.stdout.write(f"  Pruning DSLR snapshot: {old} (tenant={tenant})\n")
            run_allowed_to_fail([*dslr_cmd, "delete", "-y", old], expected_codes=None)
            deleted.append(old)
    return deleted
