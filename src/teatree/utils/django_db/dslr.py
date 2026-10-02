"""DSLR snapshot helpers for Django database provisioning.

Handles discovery, restore, and environment setup for DSLR (Django
Lightweight Snapshot Restore) snapshots used by the DB import engine in the
sibling ``importer`` module. Retention/pruning lives in ``dslr_prune``.
"""

import os
import re
import shutil
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
