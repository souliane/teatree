"""Pre-commit hook: BLUEPRINT corpus size budget (#1128).

The BLUEPRINT is functional + architectural, not a prose mirror of the
code. When the corpus (top-level ``BLUEPRINT.md`` plus the three
architectural appendices in ``docs/blueprint/``) creeps back past the
budget, that almost always means implementation prose has accreted.

This gate enforces the budget structurally so the rule does not have to
live in vigilance. It mirrors the ``skill-prose-ban`` (#140) precedent:
the rule the user keeps having to restate becomes a deterministic
pre-commit gate that fails the commit until the regression is removed.

Budget (bytes):

- Top-level ``BLUEPRINT.md``:   80 000  (~78 KB)
- ``docs/blueprint/`` corpus:  102 000  (~100 KB)
- Combined corpus total:       182 000  (~178 KB)

Escape hatch: ``BLUEPRINT_SIZE_OVERRIDE=1`` skips the check. Use only
when a planned, reviewed addition deliberately grows the corpus and the
budget itself needs to be raised in the same commit.
"""

import os
import pathlib
import subprocess
import sys

_TOP_FILE = "BLUEPRINT.md"
_APPENDIX_DIR = "docs/blueprint"

_BUDGET_TOP_LEVEL_BYTES = 80_000
# Reviewed bump (#1474): the §17.6.4 gate-2 self-rescue invariant is a
# load-bearing safety fact, and the appendix corpus was already at capacity.
# Reviewed bump (#1488): §17.6.4 gate 17 (the TaskCreated skill-loading
# gate that closes the ultracode fan-out bypass) is the same class of
# load-bearing safety fact, and the corpus was again at capacity.
_BUDGET_APPENDICES_BYTES = 103_500
_BUDGET_TOTAL_BYTES = 183_500


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _size(path: pathlib.Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _appendix_total(root: pathlib.Path) -> int:
    appendix_dir = root / _APPENDIX_DIR
    if not appendix_dir.is_dir():
        return 0
    return sum(_size(p) for p in appendix_dir.glob("*.md"))


def _blueprint_touched() -> bool:
    """True when BLUEPRINT.md or any docs/blueprint/*.md is staged."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    staged = result.stdout.splitlines()
    return any(f == _TOP_FILE or f.startswith(f"{_APPENDIX_DIR}/") for f in staged)


def main() -> int:
    if os.environ.get("BLUEPRINT_SIZE_OVERRIDE") == "1":
        return 0

    if not _blueprint_touched():
        return 0

    root = _repo_root()
    top_size = _size(root / _TOP_FILE)
    appendix_size = _appendix_total(root)
    total = top_size + appendix_size

    breaches: list[str] = []
    if top_size > _BUDGET_TOP_LEVEL_BYTES:
        breaches.append(f"{_TOP_FILE}: {top_size:,} B > budget {_BUDGET_TOP_LEVEL_BYTES:,} B")
    if appendix_size > _BUDGET_APPENDICES_BYTES:
        breaches.append(f"{_APPENDIX_DIR}/: {appendix_size:,} B > budget {_BUDGET_APPENDICES_BYTES:,} B")
    if total > _BUDGET_TOTAL_BYTES:
        breaches.append(f"corpus total: {total:,} B > budget {_BUDGET_TOTAL_BYTES:,} B")

    if not breaches:
        return 0

    print(file=sys.stderr)
    print("  BLUEPRINT corpus size budget FAILED (#1128):", file=sys.stderr)
    print(file=sys.stderr)
    for line in breaches:
        print(f"    - {line}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "  The BLUEPRINT is architectural, not a prose mirror of the code.",
        file=sys.stderr,
    )
    print(
        "  Move implementation detail to docstrings, --help text, CLAUDE.md,",
        file=sys.stderr,
    )
    print(
        "  AGENTS.md, or the issue tracker. To bypass for a reviewed bump:",
        file=sys.stderr,
    )
    print("    BLUEPRINT_SIZE_OVERRIDE=1 git commit ...", file=sys.stderr)
    print(file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
