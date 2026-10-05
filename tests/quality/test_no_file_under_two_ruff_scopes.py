"""No physical file is reachable under both ``scripts/`` and ``src/``.

ruff scans ``scripts/**`` and ``src/**`` under different per-file-ignore scopes, and a
symlinked inode is scanned under both; two scopes disagreeing on which rules apply is what
broke ``ruff check --fix`` idempotency on a pristine checkout.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ruff_walks_no_inode_under_two_scopes() -> None:
    scripts_files = {p.resolve() for p in (_REPO_ROOT / "scripts").rglob("*.py")}
    src_files = {p.resolve() for p in (_REPO_ROOT / "src").rglob("*.py")}
    shared = scripts_files & src_files
    assert not shared, f"same physical file reachable under both scripts/ and src/: {sorted(shared)}"
