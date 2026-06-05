"""Resolver building ``GateInputs`` from a ticket (#1967).

``resolve_gate_inputs`` is the wiring seam the ship-gate and §17.4 CLEAR call:
it asks the active overlay to classify the diff, reads the gate kill-switch, and
binds to the reviewed head SHA. The pure gate decision is tested separately;
this verifies the resolver threads the right inputs (classifier verdict + kill
switch) so the gate fires on the right tree.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.e2e_mandatory_gate import resolve_gate_inputs
from teatree.core.models import Ticket

_SHA = "1" * 40


class _ImpactingOverlay:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        return bool(changed_files)


class _SafeOverlay:
    def classify_customer_display_impact(self, changed_files: list[str]) -> bool:
        return False


class TestResolveGateInputs(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://example.com/i/30", overlay="t3-teatree")

    def test_impacting_overlay_marks_inputs_impacting(self) -> None:
        with (
            patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()),
            patch("teatree.core.e2e_mandatory_gate._gate_enabled", return_value=True),
        ):
            inputs = resolve_gate_inputs(self.ticket, changed_files=["app/views.py"], head_sha=_SHA)
        assert inputs.display_impacting is True
        assert inputs.head_sha == _SHA
        assert inputs.gate_enabled is True

    def test_safe_overlay_marks_inputs_non_impacting(self) -> None:
        with (
            patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_SafeOverlay()),
            patch("teatree.core.e2e_mandatory_gate._gate_enabled", return_value=True),
        ):
            inputs = resolve_gate_inputs(self.ticket, changed_files=["app/views.py"], head_sha=_SHA)
        assert inputs.display_impacting is False

    def test_kill_switch_threaded_through(self) -> None:
        with (
            patch("teatree.core.e2e_mandatory_gate.get_overlay", return_value=_ImpactingOverlay()),
            patch("teatree.core.e2e_mandatory_gate._gate_enabled", return_value=False),
        ):
            inputs = resolve_gate_inputs(self.ticket, changed_files=["app/views.py"], head_sha=_SHA)
        assert inputs.gate_enabled is False
