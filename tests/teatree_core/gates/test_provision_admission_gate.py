"""Resource-aware admission for parallel worktree provisioning (souliane/teatree#2949)."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.gates.provision_admission_gate import (
    ProvisionAdmissionVerdict,
    check_provision_admission,
    resolve_provision_max_concurrency,
    resolve_provision_ram_ceiling_percent,
)


class TestCheckProvisionAdmission(TestCase):
    def test_allows_when_ram_under_ceiling(self) -> None:
        verdict = check_provision_admission(ram_used_percent=50)
        assert verdict.ok is True
        assert verdict.reason == ""

    def test_holds_when_ram_at_or_above_ceiling(self) -> None:
        verdict = check_provision_admission(ram_used_percent=95)
        assert verdict.ok is False
        assert "ram_pressure" in verdict.reason

    def test_holds_exactly_at_ceiling(self) -> None:
        with patch(
            "teatree.core.gates.provision_admission_gate.resolve_provision_ram_ceiling_percent", return_value=85
        ):
            verdict = check_provision_admission(ram_used_percent=85)
        assert verdict.ok is False

    def test_reads_the_live_probe_when_no_sample_given(self) -> None:
        with patch("teatree.core.gates.provision_admission_gate.read_ram_used_percent", return_value=10.0):
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


class TestResolveProvisionRamCeilingPercent(TestCase):
    def test_reads_effective_settings(self) -> None:
        with patch("teatree.core.gates.provision_admission_gate.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(provision_ram_ceiling_percent=70)
            assert resolve_provision_ram_ceiling_percent() == 70

    def test_default_is_positive(self) -> None:
        assert resolve_provision_ram_ceiling_percent() > 0
