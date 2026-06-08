"""Pre-commit hook: hard cap on ``BLUEPRINT.md`` size (#1180).

The BLUEPRINT is architectural, not a prose mirror of the code. The
companion #1128 corpus-budget gate sets per-file soft budgets that only
fire when BLUEPRINT.md (or an appendix) is touched in the same commit;
this #1180 gate is the deterministic hard cap that fires whenever the
file changes and exceeds 100 KB.

Threshold: 101 KB (101 * 1024 bytes). The hook is scoped to commits
that touch ``BLUEPRINT.md`` (via ``files:`` in
``.pre-commit-config.yaml``), so it gates every growth event without
re-running on unrelated commits. Escape hatch:
``T3_BLUEPRINT_SIZE_OVERRIDE=1`` (strict ``"1"`` — empty, ``0``,
``false`` are not accepted) skips the check; use only when a planned,
reviewed addition deliberately grows the file and the cap itself is
being raised in the same commit.
"""

import os
import pathlib
import sys

_BLUEPRINT_FILE = "BLUEPRINT.md"
_THRESHOLD_BYTES = 101 * 1024
_OVERRIDE_ENV_VAR = "T3_BLUEPRINT_SIZE_OVERRIDE"


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def main() -> int:
    if os.environ.get(_OVERRIDE_ENV_VAR) == "1":
        return 0

    blueprint = _repo_root() / _BLUEPRINT_FILE
    if not blueprint.is_file():
        return 0

    size = blueprint.stat().st_size
    if size <= _THRESHOLD_BYTES:
        return 0

    print(file=sys.stderr)
    print("  BLUEPRINT.md size cap FAILED (#1180):", file=sys.stderr)
    print(
        f"    current size: {size:,} B > threshold {_THRESHOLD_BYTES:,} B",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    print(
        "  The BLUEPRINT is architectural, not a prose mirror of the code.",
        file=sys.stderr,
    )
    print(
        f"  To bypass for a reviewed, intentional bump: {_OVERRIDE_ENV_VAR}=1 git commit ...",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
