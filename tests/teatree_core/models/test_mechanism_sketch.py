"""MechanismSketch (north-star PR-6): the typed design contract + its STRUCTURAL validation.

The sketch is what a directive interpreter hands back and the human ratifies. Its
structural validation is the anti-hack teeth: a sketch is recordable only if it names
a REAL core seam (never an overlay-local patch), a valid setting, AND — the N=2 litmus
— at least one rejected alternative. The invalid cases each prove the check fires; the
round-trip proves the stored JSON rebuilds the sketch. (The activation-scope registry
check is the recorder gate's — ``tests/teatree_core/gates/test_directive_interpret_gate``.)
"""

import pytest
from django.test import TestCase

from teatree.core.models.mechanism_sketch import (
    MechanismSketch,
    MechanismSketchError,
    sketch_from_envelope,
    validate_sketch_structure,
)

#: A real core chokepoint that exists at HEAD (the PR-2 proof-case mechanism).
_CORE_CHOKEPOINT = "src/teatree/core/gates/pr_budget_gate.py::check_pr_budget"


def valid_envelope(**overrides: object) -> dict[str, object]:
    envelope: dict[str, object] = {
        "kind": "setting_policy_gate",
        "setting_key": "max_open_prs_per_repo_per_ticket",
        "setting_type": "int",
        "neutral_default": 0,
        "policy_chokepoint": _CORE_CHOKEPOINT,
        "activation_scope": "t3-teatree",
        "activation_value": 1,
        "rejected_alternatives": ["an overlay-local hook — fails N=2: a second overlay wanting max 2 needs code"],
        "acceptance_tests": ["tests/teatree_core/gates/test_pr_budget_gate.py::TestCheckPrBudget"],
        "refactors": [],
        "behavior_probe": "",
        "probe_none_reason": "covered by acceptance tests",
    }
    envelope.update(overrides)
    return envelope


class TestValidateSketchStructure(TestCase):
    def test_a_generic_sketch_naming_a_core_seam_and_a_rejected_alternative_is_valid(self) -> None:
        assert validate_sketch_structure(valid_envelope()) is None

    def test_kind_outside_the_catalog_is_rejected(self) -> None:
        finding = validate_sketch_structure(valid_envelope(kind="new_subsystem"))
        assert finding is not None
        assert "catalog" in finding

    def test_a_non_identifier_setting_key_is_rejected(self) -> None:
        finding = validate_sketch_structure(valid_envelope(setting_key="max open prs"))
        assert finding is not None
        assert "identifier" in finding

    def test_an_overlay_local_chokepoint_is_refused_as_not_a_core_seam(self) -> None:
        # The structural refusal of the one-off hack: the policy must live at a core
        # seam every overlay flows through, never inside an overlay package.
        finding = validate_sketch_structure(
            valid_envelope(policy_chokepoint="src/teatree/overlays/widget/hooks.py::cap_prs")
        )
        assert finding is not None
        assert "core seam" in finding

    def test_a_nonexistent_chokepoint_file_is_rejected(self) -> None:
        finding = validate_sketch_structure(
            valid_envelope(policy_chokepoint="src/teatree/core/gates/does_not_exist.py::f")
        )
        assert finding is not None
        assert "does not exist" in finding

    def test_a_sketch_with_no_rejected_alternative_is_incomplete_n_equals_2_litmus(self) -> None:
        # The core anti-hack assertion: the overlay-local one-off must be named and
        # rejected IN WRITING before the human ever ratifies.
        finding = validate_sketch_structure(valid_envelope(rejected_alternatives=[]))
        assert finding is not None
        assert "N=2" in finding

    def test_a_setting_policy_gate_without_acceptance_tests_is_rejected(self) -> None:
        finding = validate_sketch_structure(valid_envelope(acceptance_tests=[]))
        assert finding is not None
        assert "acceptance_tests" in finding

    def test_activation_only_needs_no_acceptance_tests(self) -> None:
        # The duplication-check branch: the mechanism already exists, so there is
        # nothing new to prove — only the activation to apply.
        envelope = valid_envelope(kind="activation_only", acceptance_tests=[])
        assert validate_sketch_structure(envelope) is None


class TestSketchRoundTrip(TestCase):
    def test_from_envelope_builds_the_typed_sketch(self) -> None:
        sketch = sketch_from_envelope(valid_envelope())
        assert isinstance(sketch, MechanismSketch)
        assert sketch.setting_key == "max_open_prs_per_repo_per_ticket"
        assert sketch.rejected_alternatives  # non-empty — the recorded N=2 decision
        assert sketch.policy_chokepoint == _CORE_CHOKEPOINT

    def test_to_dict_from_dict_is_lossless(self) -> None:
        sketch = sketch_from_envelope(valid_envelope())
        assert MechanismSketch.from_dict(sketch.to_dict()) == sketch

    def test_from_envelope_raises_on_an_invalid_sketch(self) -> None:
        with pytest.raises(MechanismSketchError):
            sketch_from_envelope(valid_envelope(rejected_alternatives=[]))
