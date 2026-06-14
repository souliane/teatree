"""The model-callable registries: lookup, idempotent re-population, re-ready safety (#2385)."""

import pytest

from teatree.core.model_registries import populate_model_registries
from teatree.core.modelkit import gate_registry


class TestGateRegistry:
    def test_register_then_get_round_trips(self) -> None:
        def fn() -> str:
            return "x"

        gate_registry.register("gate", "round_trip_probe", fn)
        assert gate_registry.get("gate", "round_trip_probe") is fn

    def test_register_is_idempotent_last_write_wins(self) -> None:
        gate_registry.register("gate", "idem_probe", lambda: "first")
        gate_registry.register("gate", "idem_probe", lambda: "second")
        assert gate_registry.get("gate", "idem_probe")() == "second"

    def test_get_unregistered_raises_clear_keyerror(self) -> None:
        with pytest.raises(KeyError, match="no 'gate' registered under 'never_registered'"):
            gate_registry.get("gate", "never_registered")

    def test_gate_resolver_cost_helpers_share_one_namespaced_dict(self) -> None:
        gate_registry.register_gate("kind_probe", lambda: "g")
        gate_registry.register_resolver("kind_probe", lambda: "r")
        # Same name, different kind -> two distinct entries, no collision.
        assert gate_registry.get_gate("kind_probe")() == "g"
        assert gate_registry.get_resolver("kind_probe")() == "r"


class TestPopulateModelRegistries:
    def test_all_model_callables_registered_after_setup(self) -> None:
        # Django setup already ran populate via CoreConfig.ready; assert the full set.
        assert gate_registry.get_gate("plan_artifact") is not None
        assert gate_registry.get_gate("local_e2e_dod") is not None
        assert gate_registry.get_gate("fix_record_dod") is not None
        assert gate_registry.get_gate("spec_coverage") is not None
        assert gate_registry.get_gate("review_context_satisfied") is not None
        assert gate_registry.get_resolver("infer_overlay_for_url") is not None
        assert gate_registry.get_resolver("resolve_overlay_name") is not None
        assert gate_registry.get("cost", "AttemptUsage") is not None
        assert gate_registry.get("cost", "CostBreakdown") is not None

    def test_re_population_is_a_noop_not_a_duplicate_key_error(self) -> None:
        # Mirrors a second AppConfig.ready (test re-entry, in-process call_command).
        populate_model_registries()
        populate_model_registries()
        # Still exactly one entry per (kind, name) — re-population overwrites,
        # it never duplicates — and the gate is still the live bare function.
        keys = [k for k in gate_registry._REGISTRY if k == ("gate", "plan_artifact")]
        assert keys == [("gate", "plan_artifact")]
        from teatree.core.gates.plan_gate import check_plan_artifact  # noqa: PLC0415

        assert gate_registry.get_gate("plan_artifact") is check_plan_artifact
