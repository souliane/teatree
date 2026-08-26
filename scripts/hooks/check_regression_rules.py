"""Blocking gate: the named regression-detector ast-grep rules find ZERO on the tree.

Runs the ``.ast-grep/blocking`` rule set (souliane/teatree#126) through the pinned
ast-grep engine and exits non-zero on any finding, so a re-introduced past bug
fails the commit/CI lane. The same scan powers ``tests/quality/
test_regression_rules.py``'s zero-findings conformance assertion — this hook is
the prek/CI enforcement face of it.

The engine is resolved hermetically (``uvx --from ast-grep-cli==<pin>``); when no
engine is available the gate FAILS LOUD (exit 2) rather than skipping — a missing
engine must surface, never silently pass (souliane/teatree#87).
"""

import sys

from teatree.quality.regression_catalog import repo_root
from teatree.quality.regression_scan import AstGrepUnavailableError, scan_findings


def main() -> int:
    blocking_dir = repo_root() / ".ast-grep" / "blocking"
    try:
        findings = scan_findings(blocking_dir)
    except AstGrepUnavailableError as exc:
        sys.stderr.write(f"regression-rules gate could not run: {exc}\n")
        return 2
    if findings:
        sys.stderr.write("blocking regression rule(s) found a re-introduced bug:\n")
        for finding in findings:
            sys.stderr.write(f"  {finding['check_id']}  {finding['path']}:{finding['start']['line']}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
