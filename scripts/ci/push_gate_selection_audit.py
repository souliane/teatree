"""Ground-truth anti-vacuity audit for the incremental push gate (#122).

The push gate SCOPES the doctest + ast-grep sweeps to the diff. This audit proves
the scoping never hides a real finding: it runs the WHOLE-TREE ast-grep scan and
the WHOLE-TREE doctest sweep, then asserts every finding/failure lies INSIDE the
scoped set the gate would have run for the same diff. Any whole-tree
finding/failure the scoped gate would have SKIPPED is a measured false negative —
the job fails LOUD (and the workflow files a tracking issue). Scoping earns trust
from evidence before the operator flips ``incremental_push_gate`` on per-overlay.

The audit always evaluates the SCOPED plan (``enabled=True``) regardless of the
flag: it measures whether scoping WOULD be safe, independent of whether it is live.
A FULL plan can never miss (it scans everything), so the audit passes trivially.
"""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.push_gate import PushGatePlan, resolve_plan
from teatree.quality.regression_catalog import repo_root
from teatree.quality.regression_scan import AstGrepUnavailableError, scan_findings
from teatree.utils.run import run_allowed_to_fail

_DOCTEST_FAIL_RE = re.compile(r"^FAILED\s+(\S+?\.py)::", re.MULTILINE)
# pytest: 0 all-passed, 1 tests-failed. 2/3/4/5 are interruption, internal error,
# usage error and nothing-collected — none of which is a doctest verdict.
_DOCTEST_VERDICT_CODES = (0, 1)


@dataclass(frozen=True)
class AuditMiss:
    dimension: str
    path: str
    detail: str


def audit_scope(
    plan: PushGatePlan,
    whole_tree_finding_paths: list[str],
    whole_tree_doctest_fail_paths: list[str],
) -> list[AuditMiss]:
    """Return every whole-tree finding/failure the SCOPED plan would have skipped.

    A FULL plan scans the whole tree, so it can never miss — an empty list. A scoped
    plan misses a finding when its path is outside the scoped ast-grep set, and a
    doctest failure when its module is outside the scoped doctest targets.
    """
    if plan.is_full:
        return []
    astgrep_scope = {str(p) for p in (plan.astgrep_scope or ())}
    doctest_scope = {str(p) for p in plan.doctest_targets}
    misses: list[AuditMiss] = []
    misses.extend(
        AuditMiss("ast-grep", path, "whole-tree blocking finding OUTSIDE the scoped ast-grep set")
        for path in whole_tree_finding_paths
        if path not in astgrep_scope
    )
    misses.extend(
        AuditMiss("doctest", path, "whole-tree doctest failure OUTSIDE the scoped doctest targets")
        for path in whole_tree_doctest_fail_paths
        if path not in doctest_scope
    )
    return misses


def _whole_tree_findings(root: Path) -> list[str]:
    try:
        findings = scan_findings(root / ".ast-grep" / "blocking", root=root, paths=None)
    except AstGrepUnavailableError as exc:
        # The audit cannot prove selection safety without the engine — fail loud.
        message = f"selection-audit: ast-grep engine unavailable ({exc}); cannot audit — failing loud"
        raise SystemExit(message) from exc
    return [f["path"] for f in findings]


def _whole_tree_doctest_failures(root: Path) -> list[str]:
    cmd = [sys.executable, "-m", "pytest", "--no-header", "-q", "--tb=no", "--doctest-modules", "src/teatree"]
    result = run_allowed_to_fail(cmd, expected_codes=None, cwd=root)
    # A sweep that never produced a verdict yields no FAILED line — the exact
    # shape of a clean tree, so the audit would certify scoping it never measured.
    if result.returncode not in _DOCTEST_VERDICT_CODES:
        message = (
            f"selection-audit: the whole-tree doctest sweep exited {result.returncode} — a collection or "
            f"infrastructure failure, not a doctest verdict; cannot audit — failing loud\n"
            f"{result.stdout}\n{result.stderr}"
        )
        raise SystemExit(message)
    failures = sorted(set(_DOCTEST_FAIL_RE.findall(result.stdout)))
    if result.returncode and not failures:
        message = (
            "selection-audit: the whole-tree doctest sweep reported failures but none could be attributed to a "
            f"module; cannot audit — failing loud\n{result.stdout}\n{result.stderr}"
        )
        raise SystemExit(message)
    return failures


def _render(misses: list[AuditMiss]) -> str:
    lines = [f"selection-audit: {len(misses)} scoped-gate MISS(es) — the scoped push gate would have SKIPPED:"]
    lines.extend(f"  [{m.dimension}] {m.path} — {m.detail}" for m in misses)
    lines.append("A whole-tree finding/failure outside the scoped set is a false negative the gate must never hide.")
    return "\n".join(lines)


def main() -> int:
    base_ref = os.environ.get("BASE_REF", "origin/main")
    root = repo_root()
    plan = resolve_plan(base_ref, enabled=True, cwd=root)
    print(plan.report())
    print(f"reason: {plan.reason}")
    misses = audit_scope(plan, _whole_tree_findings(root), _whole_tree_doctest_failures(root))
    if not misses:
        print("selection-audit: PASS — every whole-tree finding/failure is inside the scoped set (or plan is FULL).")
        return 0
    print(_render(misses), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
