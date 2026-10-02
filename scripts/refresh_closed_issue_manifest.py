"""Rewrite ``tests/quality/closed_issues.toml`` from the live issue tracker.

The closed-issue sub-gate (``tests/quality/test_incompleteness_marker_ratchet.py``)
runs entirely offline against the committed manifest. This script is the one
place that talks to the forge: run it periodically, or whenever a deferral
marker starts pointing at an issue the manifest has no state for, and commit the
result. A run that cannot reach the forge changes nothing and exits non-zero.

Only issues a marker actually points at are snapshotted. The set is derived from
the tree at run time, so adding a deferral and re-running is enough to cover it.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from teatree.quality.incompleteness_markers import issue_deferrals, scan_tree
from teatree.utils.run import run_allowed_to_fail

REPO = "souliane/teatree"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = _REPO_ROOT / "tests" / "quality" / "closed_issues.toml"

_HEADER_END = "# snapshot = "


def issue_state(number: int) -> str | None:
    result = run_allowed_to_fail(["gh", "api", f"/repos/{REPO}/issues/{number}"], expected_codes=None)
    if result.returncode != 0:
        return None
    state = json.loads(result.stdout).get("state")
    return state if state in {"open", "closed"} else None


def render(closed: list[int], still_open: list[int], preamble: str) -> str:
    today = datetime.now(UTC).date().isoformat()
    return (
        f"{preamble}{today}\n\n"
        f"closed = [{', '.join(str(n) for n in closed)}]\n"
        f"open = [{', '.join(str(n) for n in still_open)}]\n"
    )


def main() -> int:
    referenced = sorted({deferral.issue for deferral in issue_deferrals(scan_tree(_REPO_ROOT))})
    closed: list[int] = []
    still_open: list[int] = []
    for number in referenced:
        state = issue_state(number)
        if state is None:
            print(f"could not read {REPO}#{number} — manifest unchanged", file=sys.stderr)
            return 1
        (closed if state == "closed" else still_open).append(number)

    preamble = _MANIFEST.read_text(encoding="utf-8").split(_HEADER_END)[0] + _HEADER_END
    _MANIFEST.write_text(render(closed, still_open, preamble), encoding="utf-8")
    print(f"{len(closed)} closed, {len(still_open)} open across {len(referenced)} referenced issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
