"""A new default-OFF gate cannot ship without declaring what would prove it live (#4189).

``test_feature_flags.py`` already refuses a ``require_*`` toggle in neither ``FEATURE_FLAGS``
nor ``DURABLE_GATE_SETTINGS``. That closed the "governed by nobody" hole and left the one this
lane closes: twelve gates were classified, reviewed, merged — and default-OFF for months with
their evidence tables empty, because classification never asked what firing would look like.

The refusal is proven the way #4189's criterion 1 asks for — by putting an actual new
default-OFF setting into the fixture and observing the walk go red on it, not by unit-testing
the matcher against a hand-built list.
"""

import pytest

from teatree.config.feature_flags import DURABLE_GATE_SETTINGS, FEATURE_FLAGS
from teatree.config.gate_evidence import GATE_EVIDENCE, declaration_faults, ships_off, undeclared_gates
from teatree.config.schema import shipped_defaults


@pytest.fixture(scope="module")
def shipped() -> dict[str, object]:
    return dict(shipped_defaults().model_dump())


@pytest.fixture(scope="module")
def governed() -> set[str]:
    """Every gate the existing classification governs — the surface this lane walks."""
    return set(FEATURE_FLAGS) | set(DURABLE_GATE_SETTINGS)


class TestEveryDefaultOffGateDeclaresItsEvidence:
    def test_the_live_surface_is_fully_declared(self, shipped: dict[str, object], governed: set[str]) -> None:
        missing = undeclared_gates(shipped, governed)
        assert missing == (), (
            f"default-OFF gates with no declared evidence observable: {list(missing)} — "
            f"add a teatree.config.gate_evidence.GATE_EVIDENCE entry naming the observable "
            f"its being live would produce (a model, a Ticket.extra key, or an explicit none "
            f"with the reason), when it shipped, and whether anyone decided to leave it off"
        )

    def test_a_new_default_off_gate_is_refused(self, shipped: dict[str, object], governed: set[str]) -> None:
        """The RED control: a genuinely new setting, added to the fixture, must be named."""
        with_new_gate = {**shipped, "require_widget_attestation": False}
        missing = undeclared_gates(with_new_gate, governed | {"require_widget_attestation"})
        assert missing == ("require_widget_attestation",)

    def test_a_new_default_on_gate_is_not_refused(self, shipped: dict[str, object], governed: set[str]) -> None:
        """The other direction: shipping ON is not the failure this lane exists to catch."""
        with_new_gate = {**shipped, "require_widget_attestation": True}
        assert undeclared_gates(with_new_gate, governed | {"require_widget_attestation"}) == ()

    def test_a_new_default_off_mode_gate_is_refused(self, shipped: dict[str, object], governed: set[str]) -> None:
        """A tri-state posture ships off as a NAMED value, the shape ``critic_gate_mode`` has."""
        with_new_gate = {**shipped, "widget_gate_mode": "off"}
        missing = undeclared_gates(with_new_gate, governed | {"widget_gate_mode"})
        assert missing == ("widget_gate_mode",)


class TestTheDeclarationStaysHonest:
    def test_the_live_registry_is_well_formed(self) -> None:
        assert declaration_faults() == ()

    def test_it_declares_only_gates_the_classification_governs(self, governed: set[str]) -> None:
        stray = sorted(set(GATE_EVIDENCE) - governed)
        assert stray == [], f"declared but governed by neither registry: {stray}"

    def test_it_declares_only_gates_that_ship_off(self, shipped: dict[str, object]) -> None:
        on = sorted(key for key in GATE_EVIDENCE if not ships_off(shipped[key]))
        assert on == [], f"declared as shipping off but the shipped default says otherwise: {on}"

    def test_each_declared_off_value_is_the_shipped_default(self, shipped: dict[str, object]) -> None:
        """A declared off value that is not what ships would make the report read the wrong state."""
        diverged = {
            key: (entry.off_value, shipped[key])
            for key, entry in GATE_EVIDENCE.items()
            if entry.off_value != shipped[key]
        }
        assert diverged == {}

    def test_each_flag_declares_the_off_value_its_flag_entry_already_names(self) -> None:
        """One key, one answer to "what does off look like" — the two registries cannot drift."""
        diverged = {
            key: (entry.off_value, FEATURE_FLAGS[key].off_value)
            for key, entry in GATE_EVIDENCE.items()
            if key in FEATURE_FLAGS and entry.off_value != FEATURE_FLAGS[key].off_value
        }
        assert diverged == {}
