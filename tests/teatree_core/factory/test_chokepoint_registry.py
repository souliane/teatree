"""The declarative guarded-chokepoint registry — walk the DSL (PR-07)."""

import pytest

from teatree.core.factory.chokepoint_registry import (
    ChokepointResolutionError,
    GateSpec,
    GuardedChokepoint,
    all_chokepoints,
    get_chokepoint,
    register_chokepoint,
)
from teatree.core.merge.execution import merge_ticket_pr
from teatree.core.merge.sha_bind import verify_sha_bound

_MERGE_KEYSTONE = "merge_keystone"
_EXPECTED_MERGE_GATES = frozenset(
    {
        "public_repo_author_trusted",
        "clear_authorized",
        "sha_bind",
        "anti_vacuity",
        "rubric_satisfied",
        "review_verdict",
        "no_active_review_lock",
    },
)


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
