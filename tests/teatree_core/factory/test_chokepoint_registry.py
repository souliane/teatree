"""The declarative guarded-chokepoint registry — walk the DSL (PR-07).

``TestRegistryExecutionParity`` closes the §3b #1 gap: the registry used to be
descriptive-only and could silently diverge from the executed chain (a live
CI-verdict / not-draft step ran with NO ``GateSpec``). The AST parity test now
pins, both directions, that the registered ``merge_keystone`` gate callables are
EXACTLY the gate-shaped calls the keystone actually invokes.
"""

import ast
import inspect

import pytest

from teatree.core.factory.chokepoint_registry import (
    ChokepointResolutionError,
    GateSpec,
    GuardedChokepoint,
    all_chokepoints,
    get_chokepoint,
    register_chokepoint,
)
from teatree.core.merge import execution
from teatree.core.merge.execution import merge_ticket_pr
from teatree.core.merge.sha_bind import verify_sha_bound

_MERGE_KEYSTONE = "merge_keystone"
_EXPECTED_MERGE_GATES = frozenset(
    {
        "merge_provenance_trusted",
        "clear_authorized",
        "sha_bind",
        "anti_vacuity",
        "rubric_satisfied",
        "review_verdict",
        "no_active_review_lock",
        "merge_quality",
        "not_draft",
        "ci_verdict",
    },
)

# The keystone orchestrator entry points are gate-shaped calls that are NOT
# themselves gates (they RUN the gate chain); exclude them from the parity set.
_ORCHESTRATOR_ENTRIES = frozenset({"assert_merge_preconditions"})


def _gate_shaped_calls_in_execution() -> set[str]:
    """Every ``assert_*`` / ``_assert_*`` / ``verify_*`` call leaf-name in ``execution``."""
    tree = ast.parse(inspect.getsource(execution))
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name and (name.startswith(("assert_", "_assert_", "verify_"))):
            calls.add(name)
    return calls - _ORCHESTRATOR_ENTRIES


def _registered_gate_leaves() -> set[str]:
    """The leaf callable name of every registered ``merge_keystone`` gate."""
    return {gate.callable_path.rpartition(".")[2] for gate in get_chokepoint(_MERGE_KEYSTONE).required_gates}


class TestMergeKeystoneEntry:
    def test_merge_keystone_is_registered_first(self) -> None:
        assert all_chokepoints()[0].name == _MERGE_KEYSTONE

    def test_merge_keystone_lists_its_full_gate_chain(self) -> None:
        keystone = get_chokepoint(_MERGE_KEYSTONE)
        assert frozenset(keystone.gate_names()) == _EXPECTED_MERGE_GATES

    def test_merge_keystone_callable_resolves_to_merge_ticket_pr(self) -> None:
        assert get_chokepoint(_MERGE_KEYSTONE).resolve_callable() is merge_ticket_pr

    def test_verification_contract_is_populated(self) -> None:
        assert get_chokepoint(_MERGE_KEYSTONE).verification_contract.strip()


class TestWalkTheDsl:
    def test_every_gate_of_every_chokepoint_resolves_to_a_callable(self) -> None:
        for chokepoint in all_chokepoints():
            assert callable(chokepoint.resolve_callable())
            for gate in chokepoint.required_gates:
                assert callable(gate.resolve()), f"{chokepoint.name}:{gate.name} did not resolve"

    def test_sha_bind_gate_resolves_to_the_predicate(self) -> None:
        keystone = get_chokepoint(_MERGE_KEYSTONE)
        sha_bind = next(gate for gate in keystone.required_gates if gate.name == "sha_bind")
        assert sha_bind.resolve() is verify_sha_bound

    def test_resolve_gates_yields_every_gate_callable(self) -> None:
        keystone = get_chokepoint(_MERGE_KEYSTONE)
        resolved = list(keystone.resolve_gates())
        assert len(resolved) == len(keystone.required_gates)
        assert all(callable(fn) for fn in resolved)


class TestRegistryExecutionParity:
    """Registry gate names ⟺ the gate-shaped calls the keystone actually invokes (§3b #1)."""

    def test_every_registered_gate_is_actually_invoked(self) -> None:
        # Forward: registry ⊆ executed. RED before the fix — merge_quality /
        # not_draft / ci_verdict ran inline with no dedicated gate callable, so
        # ``assert_not_draft`` / ``assert_ci_not_failed`` were never called.
        registered = _registered_gate_leaves()
        invoked = _gate_shaped_calls_in_execution()
        assert registered <= invoked, f"registered but never invoked: {sorted(registered - invoked)}"

    def test_every_invoked_gate_is_registered(self) -> None:
        # Reverse: any gate-shaped call added to the keystone must be registered,
        # or this pins the drift (a gate added/removed in execution.py now fails).
        registered = _registered_gate_leaves()
        invoked = _gate_shaped_calls_in_execution()
        assert invoked <= registered, f"invoked but unregistered: {sorted(invoked - registered)}"

    def test_new_floor_gates_resolve_to_the_execution_callables(self) -> None:
        keystone = get_chokepoint(_MERGE_KEYSTONE)
        by_name = {gate.name: gate for gate in keystone.required_gates}
        assert by_name["not_draft"].resolve() is execution.assert_not_draft
        assert by_name["ci_verdict"].resolve() is execution.assert_ci_not_failed


class TestResolutionFailsLoud:
    def test_unknown_chokepoint_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="no chokepoint registered"):
            get_chokepoint("does-not-exist")

    def test_bad_dotted_path_raises_resolution_error(self) -> None:
        with pytest.raises(ChokepointResolutionError):
            GateSpec(name="bogus", callable_path="not_a_dotted_path", purpose="x").resolve()

    def test_non_callable_target_raises_resolution_error(self) -> None:
        with pytest.raises(ChokepointResolutionError):
            GateSpec(name="bogus", callable_path="teatree.core.merge.sha_bind.__doc__", purpose="x").resolve()

    def test_missing_module_raises_resolution_error(self) -> None:
        with pytest.raises(ChokepointResolutionError):
            GateSpec(name="bogus", callable_path="teatree.no_such_module_xyz.fn", purpose="x").resolve()

    def test_register_is_idempotent_overwrite_by_name(self) -> None:
        original = get_chokepoint(_MERGE_KEYSTONE)
        replacement = GuardedChokepoint(
            name=_MERGE_KEYSTONE,
            callable_path="teatree.core.merge.execution.merge_ticket_pr",
            verification_contract="x",
            required_gates=(),
        )
        try:
            register_chokepoint(replacement)
            assert get_chokepoint(_MERGE_KEYSTONE) is replacement
        finally:
            register_chokepoint(original)
