"""Pre-commit hook: hard cap on ``BLUEPRINT.md`` size (#1180).

The BLUEPRINT is architectural, not a prose mirror of the code. The
companion #1128 corpus-budget gate sets per-file soft budgets that only
fire when BLUEPRINT.md (or an appendix) is touched in the same commit;
this #1180 gate is the deterministic hard cap that fires whenever the
file changes and exceeds 112 KB.

Threshold: 112 KB (112 * 1024 bytes). The hook is scoped to commits
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
# Raised 108 -> 109 KB (#2217): documenting the third external-delivery
# dispatch chokepoint + the lease-refresh-on-FSM-seam behaviour is legit
# architectural growth (a new chokepoint + a new seam), not prose bloat.
# Raised 109 -> 110 KB (#2217): the filesystem-evidence double-dispatch guard
# (`core.worktree_collision` + the `workspace ticket` chokepoint) is a new
# module and a new defense distinct from the DB lease — legit architecture.
# Raised 110 -> 111 KB (#2220): the provisioning time-box + loud-alert is a new
# module (`core.provision_timebox`) and a new lifecycle invariant (a long step
# aborts+alerts, never hangs) — legit architecture, not prose bloat.
# Raised 111 -> 113 KB (#2216): documenting the per-skill model floor +
# spawn-model merge chokepoint and the session-level effort/model pins is
# legit architectural growth, stacking on #2220's provisioning time-box section
# after merging origin/main into the #2216 branch.
_THRESHOLD_BYTES = 113 * 1024
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
