# test-path: cross-cutting
"""Ground-truth audit logic for the incremental push gate (#122).

Pins that ``audit_scope`` measures a scoped-gate MISS correctly: a FULL plan can
never miss; a scoped plan misses any whole-tree finding/failure outside its scope,
and passes when everything is inside scope. This is the anti-vacuity trust-builder
that must fail LOUD before the operator flips the flag on.
"""

from pathlib import Path

from scripts.ci.push_gate_selection_audit import audit_scope
from teatree.quality.push_gate import WHOLE_TREE_DOCTEST, PushGatePlan

_FULL = PushGatePlan(
    is_full=True, reason="full", doctest_targets=(WHOLE_TREE_DOCTEST,), astgrep_scope=None, enabled=True
)
_SCOPED = PushGatePlan(
    is_full=False,
    reason="scoped",
    doctest_targets=(Path("src/teatree/a.py"),),
    astgrep_scope=(Path("src/teatree/a.py"), Path("tests/teatree_x/test_a.py")),
    enabled=True,
)


class TestAuditScope:
    def test_full_plan_never_misses(self) -> None:
        assert audit_scope(_FULL, ["src/teatree/z.py"], ["src/teatree/z.py"]) == []

    def test_astgrep_finding_outside_scope_is_a_miss(self) -> None:
        misses = audit_scope(_SCOPED, ["src/teatree/z.py"], [])
        assert len(misses) == 1
        assert misses[0].dimension == "ast-grep"
        assert misses[0].path == "src/teatree/z.py"

    def test_astgrep_finding_inside_scope_is_not_a_miss(self) -> None:
        assert audit_scope(_SCOPED, ["src/teatree/a.py"], []) == []

    def test_doctest_failure_outside_scope_is_a_miss(self) -> None:
        misses = audit_scope(_SCOPED, [], ["src/teatree/z.py"])
        assert len(misses) == 1
        assert misses[0].dimension == "doctest"

    def test_doctest_failure_inside_scope_is_not_a_miss(self) -> None:
        assert audit_scope(_SCOPED, [], ["src/teatree/a.py"]) == []

    def test_clean_whole_tree_passes(self) -> None:
        assert audit_scope(_SCOPED, [], []) == []
