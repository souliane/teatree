"""A trusted internal pytest node-runner — the directive VERIFYING acceptance re-run (north-star PR-7).

The directive loop re-runs a ratified sketch's acceptance-test node ids at the merged
tree (design evidence class 2), node-scoped and cheap, to confirm the mechanism the
plan was required to add is present AND green — belt-and-suspenders over the full CI
that already ran at merge. The run goes through the typed ``teatree.utils.run`` egress
wrapper (the src shell-out chokepoint); ``expected_codes=None`` lets pytest's non-zero
(red) exit return normally, since a red run is the answer, not an error.
"""

import sys

from teatree.utils.run import run_allowed_to_fail


def run_acceptance_tests(node_ids: list[str]) -> bool:
    """Run *node_ids* through the current interpreter's pytest; green iff it exits 0.

    Uses ``sys.executable -m pytest`` (an absolute interpreter path) so the run
    targets the same environment the loop runs in, node-scoped with coverage off.
    """
    result = run_allowed_to_fail(
        [sys.executable, "-m", "pytest", "--no-cov", "-q", "-p", "no:cacheprovider", *node_ids],
        expected_codes=None,
    )
    return result.returncode == 0
