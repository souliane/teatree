"""Resource-aware admission for parallel worktree provisioning (souliane/teatree#2949)."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.admission_pressure import MachineSignal
from teatree.core.gates import provision_admission_gate
from teatree.core.gates.provision_admission_gate import (
    ProvisionAdmissionVerdict,
    check_provision_admission,
    resolve_provision_max_concurrency,
)


@pytest.fixture(autouse=True)
def _healthy_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision_admission_gate, "read_disk_used_percent", lambda: 20.0)


class TestCheckProvisionAdmission(TestCase):
    @staticmethod
    def _machine(available_gb: float) -> MachineSignal:
        return MachineSignal(cores=8, load1=1.0, ram_available_gb=available_gb)

    def test_holds_on_the_governor_memory_floor_and_names_unknown(self) -> None:
        floor = MachineSignal(cores=8, load1=1.0, ram_available_gb=4.0, memory_source="container reading")
        unreadable = MachineSignal(cores=8, load1=1.0, ram_available_gb=None, memory_source="container reading")
        assert "4 GB floor" in check_provision_admission(machine=floor).reason
        verdict = check_provision_admission(machine=unreadable)
        assert not verdict.ok
        assert "unknown; container reading" in verdict.reason

    def test_holds_on_critical_disk_before_provisioning(self) -> None:
        verdict = check_provision_admission(machine=self._machine(8), disk_used_percent=96)
        assert not verdict.ok
        assert "disk_pressure" in verdict.reason

    def test_unmeasurable_disk_does_not_wedge_provisioning(self) -> None:
        verdict = check_provision_admission(machine=self._machine(8), disk_probe=lambda: None)
        assert verdict.ok

    def test_healthy_disk_keeps_the_existing_ram_refusal(self) -> None:
        verdict = check_provision_admission(machine=self._machine(2), disk_used_percent=20)
        assert not verdict.ok
        assert "memory_pressure" in verdict.reason

    def test_allows_when_ram_under_ceiling(self) -> None:
        verdict = check_provision_admission(machine=self._machine(8))
        assert verdict.ok is True
        assert verdict.reason == ""

    def test_holds_when_ram_at_or_above_ceiling(self) -> None:
        verdict = check_provision_admission(machine=self._machine(2))
        assert verdict.ok is False
        assert "memory_pressure" in verdict.reason

    def test_holds_exactly_at_floor(self) -> None:
        verdict = check_provision_admission(machine=self._machine(4))
        assert verdict.ok is False

    def test_reads_the_live_probe_when_no_sample_given(self) -> None:
        with patch("teatree.core.gates.provision_admission_gate.read_machine_signal", return_value=self._machine(8)):
            verdict = check_provision_admission()
        assert verdict.ok is True

    def test_verdict_helpers(self) -> None:
        assert ProvisionAdmissionVerdict.allow() == ProvisionAdmissionVerdict(ok=True, reason="")
        assert ProvisionAdmissionVerdict.hold("x") == ProvisionAdmissionVerdict(ok=False, reason="x")


class TestResolveProvisionMaxConcurrency(TestCase):
    def test_zero_setting_auto_derives_from_ncpu(self) -> None:
        with (
            patch("teatree.core.gates.provision_admission_gate.get_effective_settings") as mock_settings,
            patch("teatree.core.gates.provision_admission_gate.default_provision_concurrency", return_value=4),
        ):
            mock_settings.return_value = MagicMock(provision_max_concurrency=0)
            assert resolve_provision_max_concurrency() == 4

    def test_pinned_positive_value_wins(self) -> None:
        with patch("teatree.core.gates.provision_admission_gate.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(provision_max_concurrency=7)
            assert resolve_provision_max_concurrency() == 7
