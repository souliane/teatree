"""Tests for :class:`ResourcePressureScanner` — disk/RAM auto-free scanner (#128).

The scanner measures *absolute* free disk (``os.statvfs``) and reclaimable
RAM (``/proc/meminfo`` on Linux, ``vm_stat`` on macOS) every cadence window,
classifies a pressure level, and emits ``resource.*`` signals. The freeing
itself is the mechanical handler's job (tested in
``test_resource_pressure_mechanical``); these tests pin the measurement
parsing, the per-platform RAM reader dispatch and its INERT signal (#4104),
the absolute-bytes classification (the APFS / "99 % RAM used" traps), the
cadence + freeing-rate-limit gates, and the consecutive-CRITICAL counter.

Only the unstoppable externals are mocked: ``os.statvfs`` (a real fill-up
can't be staged on the CI disk), ``vm_stat`` output and ``platform.system``
(host-dependent), and the clock via marker timestamps. The Linux reader is
exercised against a REAL ``/proc/meminfo``-shaped file on disk. The marker,
signals, and classification run against the real Django ORM + real scanner.
"""

import datetime as _dt
import platform
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.resource_pressure_marker import ResourcePressureMarker
from teatree.loop.scanners.resource_pressure import (
    ResourcePressureScanner,
    _parse_vm_stat_avail_gb,
    read_disk_free_gb,
    read_ram_avail_gb,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_GIB = 1024 * 1024 * 1024
_MODULE = "teatree.loop.scanners.resource_pressure"

# A representative ``vm_stat`` capture (16 KB pages). free=13407, inactive=288140,
# purgeable=10000, speculative=5000 → 316547 reclaimable pages * 16384 bytes.
_VM_STAT_SAMPLE = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               13407.
Pages active:                            287425.
Pages inactive:                          288140.
Pages speculative:                         5000.
Pages throttled:                              0.
Pages wired down:                        313028.
Pages purgeable:                          10000.
"""

# A real ``/proc/meminfo`` head from a 30 GiB Linux box under the pressure that
# OOM-killed the worker three times (#4104): 1 GiB MemAvailable, below the 1.5 GB
# CRITICAL threshold. MemFree is deliberately far lower than MemAvailable — the
# kernel's MemAvailable already credits reclaimable page cache, which is exactly
# why it, not MemFree, is the honest figure.
_MEMINFO_CRITICAL = """MemTotal:       31457280 kB
MemFree:          204800 kB
MemAvailable:    1048576 kB
Buffers:           81920 kB
Cached:          1130496 kB
SwapTotal:             0 kB
"""

# The same box with room to breathe: 20 GiB MemAvailable.
_MEMINFO_HEALTHY = """MemTotal:       31457280 kB
MemFree:        18874368 kB
MemAvailable:   20971520 kB
Buffers:          409600 kB
Cached:          2097152 kB
"""


class VmStatParsingTests(TestCase):
    """The ``vm_stat`` parser sums only the reclaimable page classes."""

    def test_sums_reclaimable_page_classes(self) -> None:
        avail_gb = _parse_vm_stat_avail_gb(_VM_STAT_SAMPLE)
        expected_pages = 13407 + 288140 + 10000 + 5000
        assert avail_gb is not None
        assert avail_gb == pytest.approx(expected_pages * 16384 / _GIB)

    def test_excludes_wired_active_from_reclaimable(self) -> None:
        """Wired + active pages are genuinely committed — never counted as available."""
        avail_gb = _parse_vm_stat_avail_gb(_VM_STAT_SAMPLE)
        # If active(287425)+wired(313028) leaked in, the figure would roughly double.
        committed_pages = 287425 + 313028
        assert avail_gb is not None
        assert avail_gb < committed_pages * 16384 / _GIB

    def test_unparsable_output_returns_none(self) -> None:
        assert _parse_vm_stat_avail_gb("garbage with no page size") is None

    def test_missing_all_reclaimable_labels_returns_none(self) -> None:
        header_only = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages wired down: 100.\n"
        assert _parse_vm_stat_avail_gb(header_only) is None

    def test_page_size_header_without_digit_returns_none(self) -> None:
        """A 'page size of' line whose tokens are all non-numeric yields no page size."""
        no_digit = "Mach Virtual Memory Statistics: (page size of N/A bytes)\nPages free: 100.\n"
        assert _parse_vm_stat_avail_gb(no_digit) is None

    def test_label_with_non_numeric_value_is_ignored(self) -> None:
        """A reclaimable label whose value is non-numeric contributes zero, not a crash."""
        mixed = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free: not-a-number\n"
            "Pages inactive: 100.\n"
        )
        avail = _parse_vm_stat_avail_gb(mixed)
        assert avail == pytest.approx(100 * 16384 / _GIB)


class DiskMeasurementTests(TestCase):
    """Disk free is absolute bytes from ``statvfs`` — never a percent of total."""

    def test_reads_absolute_free_bytes_not_percent(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        # APFS trap: 460 G nominal total, 7 G free. f_bavail*f_frsize = the 7 G,
        # which is what matters — a percent-of-total would read "98 % free = fine".
        class _Stat:
            f_bavail = 7 * _GIB // 4096
            f_frsize = 4096

        with patch("teatree.loop.scanners.resource_pressure.os.statvfs", return_value=_Stat()):
            free_gb = read_disk_free_gb("/")
        assert free_gb == pytest.approx(7.0, abs=0.01)

    def test_statvfs_oserror_returns_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch("teatree.loop.scanners.resource_pressure.os.statvfs", side_effect=OSError):
            assert read_disk_free_gb("/") is None


class MacosRamMeasurementTests(TestCase):
    """On macOS, RAM available is the reclaimable ``vm_stat`` sum — never raw percent-used."""

    def setUp(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        super().setUp()
        self.enterContext(patch(f"{_MODULE}.platform.system", return_value="Darwin"))

    def test_vm_stat_absent_returns_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch(f"{_MODULE}.shutil.which", return_value=None):
            assert read_ram_avail_gb() is None

    def test_vm_stat_nonzero_exit_returns_none(self) -> None:
        from subprocess import CompletedProcess  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/vm_stat"),
            patch(
                f"{_MODULE}.run_allowed_to_fail",
                return_value=CompletedProcess(args=["vm_stat"], returncode=1, stdout="", stderr="boom"),
            ),
        ):
            assert read_ram_avail_gb() is None

    def test_vm_stat_invocation_error_returns_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/vm_stat"),
            patch(f"{_MODULE}.run_allowed_to_fail", side_effect=OSError),
        ):
            assert read_ram_avail_gb() is None

    def test_vm_stat_success_parses_output(self) -> None:
        from subprocess import CompletedProcess  # noqa: PLC0415
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/vm_stat"),
            patch(
                f"{_MODULE}.run_allowed_to_fail",
                return_value=CompletedProcess(args=["vm_stat"], returncode=0, stdout=_VM_STAT_SAMPLE, stderr=""),
            ),
        ):
            avail = read_ram_avail_gb()
        assert avail is not None
        assert avail == pytest.approx((13407 + 288140 + 10000 + 5000) * 16384 / _GIB)


class LinuxRamMeasurementTests(TestCase):
    """#4104 — on Linux the reader is ``/proc/meminfo``'s ``MemAvailable``, not ``vm_stat``.

    ``vm_stat`` does not exist on Linux, so the pre-#4104 reader returned ``None``
    on every pass and the whole RAM ladder was skipped on the platform this
    deployment actually runs on.
    """

    def setUp(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        super().setUp()
        self.enterContext(patch(f"{_MODULE}.platform.system", return_value="Linux"))

    def _meminfo_file(self, body: str) -> str:
        path = Path(self.enterContext(TemporaryDirectory())) / "meminfo"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def _read_with(self, body: str) -> float | None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch(f"{_MODULE}._MEMINFO_PATH", self._meminfo_file(body)):
            return read_ram_avail_gb()

    def test_reads_mem_available_not_mem_free(self) -> None:
        """MemAvailable already credits reclaimable page cache — MemFree understates by 4x here."""
        assert self._read_with(_MEMINFO_HEALTHY) == pytest.approx(20971520 * 1024 / _GIB)

    def test_reads_critical_figure(self) -> None:
        assert self._read_with(_MEMINFO_CRITICAL) == pytest.approx(1.0, abs=0.001)

    def test_meminfo_without_mem_available_returns_none(self) -> None:
        assert self._read_with("MemTotal:       31457280 kB\nMemFree:          204800 kB\n") is None

    def test_unreadable_meminfo_returns_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch(f"{_MODULE}._MEMINFO_PATH", "/nonexistent/meminfo"):
            assert read_ram_avail_gb() is None

    def test_never_shells_out_to_vm_stat_on_linux(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch(f"{_MODULE}._MEMINFO_PATH", self._meminfo_file(_MEMINFO_HEALTHY)),
            patch(f"{_MODULE}.run_allowed_to_fail") as run,
        ):
            read_ram_avail_gb()
        run.assert_not_called()


class UnknownPlatformRamMeasurementTests(TestCase):
    """A platform with neither reader yields ``None`` — which the scanner must SAY, not swallow."""

    def test_returns_none(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch(f"{_MODULE}.platform.system", return_value="SunOS"):
            assert read_ram_avail_gb() is None

    def test_this_host_can_answer(self) -> None:
        """A probe that cannot answer on the host it runs on is the #4104 defect itself."""
        assert platform.system() in {"Linux", "Darwin"}, "test host is neither supported platform"
        assert read_ram_avail_gb() is not None, "the RAM ladder is inert on this host"


class _ScannerHarness(TestCase):
    """Shared helper that stubs the two measurement functions on the scanner."""

    def _scan_with(
        self,
        *,
        disk_gb: float,
        ram_gb: float,
        scanner: ResourcePressureScanner | None = None,
    ) -> list:
        from unittest.mock import patch  # noqa: PLC0415

        scanner = scanner or ResourcePressureScanner()
        with (
            patch("teatree.loop.scanners.resource_pressure.read_disk_free_gb", return_value=disk_gb),
            patch("teatree.loop.scanners.resource_pressure.read_ram_avail_gb", return_value=ram_gb),
        ):
            return scanner.scan()


class ClassificationTests(_ScannerHarness):
    """The L0/L1/L2 decision ladder on absolute readings."""

    def test_normal_both_above_warn_emits_nothing(self) -> None:
        signals = self._scan_with(disk_gb=100.0, ram_gb=10.0)
        assert signals == []

    def test_apfs_trap_low_free_classifies_critical_not_normal(self) -> None:
        """7 G free on a 460 G APFS container is CRITICAL — proves absolute, not percent."""
        signals = self._scan_with(disk_gb=7.0, ram_gb=10.0)
        kinds = [s.kind for s in signals]
        assert "resource.cleanup_needed" in kinds
        disk_sig = next(s for s in signals if s.payload.get("resource") == "disk")
        assert disk_sig.payload["level"] == "critical"

    def test_disk_warn_band_is_advisory_only(self) -> None:
        signals = self._scan_with(disk_gb=18.0, ram_gb=10.0)
        assert len(signals) == 1
        assert signals[0].kind == "resource.pressure_warn"
        assert signals[0].payload["resource"] == "disk"

    def test_ram_warn_band_is_advisory_only(self) -> None:
        signals = self._scan_with(disk_gb=100.0, ram_gb=2.4)
        assert len(signals) == 1
        assert signals[0].kind == "resource.pressure_warn"
        assert signals[0].payload["resource"] == "ram"

    def test_ram_critical_emits_cleanup_needed(self) -> None:
        signals = self._scan_with(disk_gb=100.0, ram_gb=1.0)
        assert any(s.kind == "resource.cleanup_needed" and s.payload["resource"] == "ram" for s in signals)

    def test_both_critical_emits_two_cleanup_signals(self) -> None:
        signals = self._scan_with(disk_gb=5.0, ram_gb=1.0)
        resources = sorted(s.payload["resource"] for s in signals if s.kind == "resource.cleanup_needed")
        assert resources == ["disk", "ram"]


class LinuxRamLadderTests(TestCase):
    """#4104 — the RAM ladder fires end-to-end on a Linux host below its thresholds.

    The disk reader is stubbed (a real fill-up can't be staged); the RAM figure
    comes through the REAL reader off a real ``/proc/meminfo``-shaped file, so
    this covers the reader, the dispatch, and the classification together.
    """

    def _scan_at(self, body: str) -> list:
        from unittest.mock import patch  # noqa: PLC0415

        path = Path(self.enterContext(TemporaryDirectory())) / "meminfo"
        path.write_text(body, encoding="utf-8")
        with (
            patch(f"{_MODULE}.platform.system", return_value="Linux"),
            patch(f"{_MODULE}._MEMINFO_PATH", str(path)),
            patch(f"{_MODULE}.read_disk_free_gb", return_value=100.0),
        ):
            return ResourcePressureScanner().scan()

    def test_below_critical_mem_available_emits_ram_cleanup(self) -> None:
        signals = self._scan_at(_MEMINFO_CRITICAL)
        ram = [s for s in signals if s.payload.get("resource") == "ram"]
        assert ram, f"no RAM signal at 1 GB available — the ladder is inert; got {[s.kind for s in signals]}"
        assert ram[0].kind == "resource.cleanup_needed"
        assert ram[0].payload["level"] == "critical"
        assert ram[0].payload["free_gb"] == pytest.approx(1.0, abs=0.001)

    def test_warn_band_mem_available_emits_ram_advisory(self) -> None:
        warn_band = _MEMINFO_CRITICAL.replace("MemAvailable:    1048576 kB", "MemAvailable:    2097152 kB")
        signals = self._scan_at(warn_band)
        ram = [s for s in signals if s.payload.get("resource") == "ram"]
        assert ram, "no RAM signal at 2 GB available — the WARN ladder is inert"
        assert ram[0].kind == "resource.pressure_warn"

    def test_healthy_mem_available_stays_silent(self) -> None:
        assert self._scan_at(_MEMINFO_HEALTHY) == []

    def test_measured_figure_reaches_the_marker(self) -> None:
        self._scan_at(_MEMINFO_HEALTHY)
        assert ResourcePressureMarker.load().last_ram_avail_gb == pytest.approx(20971520 * 1024 / _GIB)


class RamProbeInertTests(TestCase):
    """#4104 — a probe that cannot answer on this host must SAY so, never report nothing.

    An unprotected box has to be distinguishable from a healthy one: silently
    returning zero signals is what let a Linux deployment run for months with the
    RAM half of the scanner structurally switched off.
    """

    def _scan_without_ram_reader(self, *, disk_gb: float | None = 100.0) -> list:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.platform.system", return_value="SunOS"),
            patch(f"{_MODULE}.read_disk_free_gb", return_value=disk_gb),
        ):
            return ResourcePressureScanner().scan()

    def test_no_ram_reader_emits_probe_inert(self) -> None:
        signals = self._scan_without_ram_reader()
        assert [s.kind for s in signals] == ["resource.probe_inert"], (
            f"an inert RAM ladder must be reported, got {[s.kind for s in signals]}"
        )
        assert signals[0].payload["resource"] == "ram"
        assert signals[0].payload["platform"] == "SunOS"

    def test_probe_inert_when_no_resource_is_readable(self) -> None:
        """Even with the disk read also failing, the inert RAM ladder is still reported."""
        signals = self._scan_without_ram_reader(disk_gb=None)
        assert [s.kind for s in signals] == ["resource.probe_inert"]

    def test_inert_probe_never_reads_as_zero_available(self) -> None:
        """An unanswerable probe must never trip a CRITICAL freeing pass — that is the opposite of fail-safe."""
        signals = self._scan_without_ram_reader()
        assert all(s.kind != "resource.cleanup_needed" for s in signals)
        assert ResourcePressureMarker.load().consecutive_critical == 0

    def test_unreadable_ram_is_recorded_as_null_not_infinity(self) -> None:
        self._scan_without_ram_reader()
        marker = ResourcePressureMarker.load()
        assert marker.last_ram_avail_gb is None
        assert marker.last_disk_free_gb == pytest.approx(100.0)

    def test_readable_ram_emits_no_inert_signal(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.read_disk_free_gb", return_value=100.0),
            patch(f"{_MODULE}.read_ram_avail_gb", return_value=10.0),
        ):
            assert ResourcePressureScanner().scan() == []


class ThresholdBoundaryTests(_ScannerHarness):
    """Free exactly AT the crit threshold is NOT critical; one step below IS."""

    def test_disk_exactly_at_crit_is_not_critical(self) -> None:
        signals = self._scan_with(disk_gb=10.0, ram_gb=100.0)
        # 10.0 == disk_crit_free_gb → not below → WARN band (10 < 25), not cleanup.
        assert all(s.kind != "resource.cleanup_needed" for s in signals)

    def test_disk_just_below_crit_is_critical(self) -> None:
        signals = self._scan_with(disk_gb=9.99, ram_gb=100.0)
        assert any(s.kind == "resource.cleanup_needed" for s in signals)

    def test_disk_exactly_at_warn_is_not_warn(self) -> None:
        signals = self._scan_with(disk_gb=25.0, ram_gb=100.0)
        assert signals == []

    def test_ram_exactly_at_crit_is_not_critical(self) -> None:
        signals = self._scan_with(disk_gb=100.0, ram_gb=1.5)
        assert all(s.kind != "resource.cleanup_needed" for s in signals)

    def test_ram_just_below_crit_is_critical(self) -> None:
        signals = self._scan_with(disk_gb=100.0, ram_gb=1.49)
        assert any(s.kind == "resource.cleanup_needed" for s in signals)


class CadenceGateTests(_ScannerHarness):
    """Measurement is throttled to once per ``cadence_minutes``."""

    def test_recent_measurement_short_circuits_scan(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.last_run_at = timezone.now() - _dt.timedelta(minutes=2)
        marker.save(update_fields=["last_run_at"])

        scanner = ResourcePressureScanner(cadence_minutes=5)
        signals = self._scan_with(disk_gb=5.0, ram_gb=1.0, scanner=scanner)
        assert signals == []

    def test_elapsed_cadence_runs_measurement(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.last_run_at = timezone.now() - _dt.timedelta(minutes=10)
        marker.save(update_fields=["last_run_at"])

        scanner = ResourcePressureScanner(cadence_minutes=5)
        signals = self._scan_with(disk_gb=5.0, ram_gb=1.0, scanner=scanner)
        assert any(s.kind == "resource.cleanup_needed" for s in signals)

    def test_measurement_persisted_to_marker(self) -> None:
        self._scan_with(disk_gb=42.0, ram_gb=7.5)
        marker = ResourcePressureMarker.load()
        assert marker.last_disk_free_gb == pytest.approx(42.0)
        assert marker.last_ram_avail_gb == pytest.approx(7.5)
        assert marker.last_run_at is not None


class FreeingRateLimitTests(_ScannerHarness):
    """A freeing pass runs at most once per ``min_free_interval_minutes`` (anti-thrash)."""

    def test_recent_free_downgrades_critical_to_advisory(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.last_freed_at = timezone.now() - _dt.timedelta(minutes=5)
        marker.save(update_fields=["last_freed_at"])

        scanner = ResourcePressureScanner(min_free_interval_minutes=30)
        signals = self._scan_with(disk_gb=5.0, ram_gb=1.0, scanner=scanner)
        # Still surfaced (the user sees the pressure) but NO second cleanup kicked off.
        assert signals, "critical pressure should still surface"
        assert all(s.kind != "resource.cleanup_needed" for s in signals)
        assert all(s.kind == "resource.pressure_warn" for s in signals)

    def test_elapsed_free_interval_allows_cleanup(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.last_freed_at = timezone.now() - _dt.timedelta(minutes=45)
        marker.save(update_fields=["last_freed_at"])

        scanner = ResourcePressureScanner(min_free_interval_minutes=30)
        signals = self._scan_with(disk_gb=5.0, ram_gb=1.0, scanner=scanner)
        assert any(s.kind == "resource.cleanup_needed" for s in signals)


class ConsecutiveCriticalTests(_ScannerHarness):
    """The sustained-CRITICAL-RAM counter increments and resets."""

    def test_ram_critical_increments_counter(self) -> None:
        self._scan_with(disk_gb=100.0, ram_gb=1.0)
        assert ResourcePressureMarker.load().consecutive_critical == 1

    def test_consecutive_ram_critical_accumulates(self) -> None:
        scanner = ResourcePressureScanner(cadence_minutes=0)
        self._scan_with(disk_gb=100.0, ram_gb=1.0, scanner=scanner)
        self._scan_with(disk_gb=100.0, ram_gb=1.0, scanner=scanner)
        assert ResourcePressureMarker.load().consecutive_critical == 2

    def test_ram_recovery_resets_counter(self) -> None:
        scanner = ResourcePressureScanner(cadence_minutes=0)
        self._scan_with(disk_gb=100.0, ram_gb=1.0, scanner=scanner)
        self._scan_with(disk_gb=100.0, ram_gb=10.0, scanner=scanner)
        assert ResourcePressureMarker.load().consecutive_critical == 0

    def test_cleanup_payload_carries_consecutive_count(self) -> None:
        scanner = ResourcePressureScanner(cadence_minutes=0)
        self._scan_with(disk_gb=100.0, ram_gb=1.0, scanner=scanner)
        signals = self._scan_with(disk_gb=100.0, ram_gb=1.0, scanner=scanner)
        ram_sig = next(s for s in signals if s.payload.get("resource") == "ram")
        assert ram_sig.payload["consecutive_critical"] == 2


class MeasurementUnavailableTests(TestCase):
    """An unmeasurable resource never trips CRITICAL — but the RAM half is no longer silent (#4104)."""

    def _scan_with_reads(self, *, disk: float | None, ram: float | None) -> list:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch(f"{_MODULE}.read_disk_free_gb", return_value=disk),
            patch(f"{_MODULE}.read_ram_avail_gb", return_value=ram),
        ):
            return ResourcePressureScanner().scan()

    def test_both_measurements_none_reports_the_ram_ladder_inert(self) -> None:
        assert [s.kind for s in self._scan_with_reads(disk=None, ram=None)] == ["resource.probe_inert"]

    def test_missing_ram_read_does_not_trip_critical(self) -> None:
        """A missing RAM read must not spuriously trip a CRITICAL freeing pass."""
        signals = self._scan_with_reads(disk=100.0, ram=None)
        assert all(s.kind != "resource.cleanup_needed" for s in signals)

    def test_missing_disk_read_still_classifies_ram(self) -> None:
        signals = self._scan_with_reads(disk=None, ram=1.0)
        assert [s.payload["resource"] for s in signals] == ["ram"]
        assert signals[0].kind == "resource.cleanup_needed"


class CleanupPayloadTests(_ScannerHarness):
    """The cleanup signal carries the allow-lists + destructive flags for the handler."""

    def test_payload_defaults_destructive_flags_off(self) -> None:
        signals = self._scan_with(disk_gb=5.0, ram_gb=100.0)
        disk_sig = next(s for s in signals if s.kind == "resource.cleanup_needed")
        assert disk_sig.payload["allow_destructive_disk"] is False
        assert disk_sig.payload["allow_destructive_ram"] is False

    def test_payload_carries_configured_allowlist(self) -> None:
        scanner = ResourcePressureScanner(
            disk_cache_allowlist=("~/.cache/pre-commit",),
            allow_destructive_disk=True,
            ram_kill_allowlist=("Brave.*Renderer",),
        )
        signals = self._scan_with(disk_gb=5.0, ram_gb=100.0, scanner=scanner)
        disk_sig = next(s for s in signals if s.kind == "resource.cleanup_needed")
        assert disk_sig.payload["disk_cache_allowlist"] == ["~/.cache/pre-commit"]
        assert disk_sig.payload["allow_destructive_disk"] is True
        assert disk_sig.payload["ram_kill_allowlist"] == ["Brave.*Renderer"]


class ResilienceTests(_ScannerHarness):
    """A marker load failure can never crash the tick."""

    def test_marker_load_failure_returns_empty(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        scanner = ResourcePressureScanner()
        with patch(
            "teatree.core.models.resource_pressure_marker.ResourcePressureMarker.load",
            side_effect=RuntimeError("db down"),
        ):
            assert scanner.scan() == []


class MarkerSwallowTests(_ScannerHarness):
    """Persistence failures inside the scan are swallowed, never crash the tick."""

    def test_record_measurement_failure_is_swallowed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with patch(
            "teatree.core.models.resource_pressure_marker.ResourcePressureMarker.record_measurement",
            side_effect=RuntimeError("write failed"),
        ):
            # Classification still proceeds even though the measurement persist failed.
            signals = self._scan_with(disk_gb=5.0, ram_gb=100.0)
        assert any(s.kind == "resource.cleanup_needed" for s in signals)

    def test_consecutive_critical_save_failure_is_swallowed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        marker = ResourcePressureMarker.load()
        with patch.object(type(marker), "save", side_effect=RuntimeError("db down")):
            # _track_consecutive_critical's save raises — swallowed, scan returns.
            scanner = ResourcePressureScanner()
            with (
                patch("teatree.loop.scanners.resource_pressure.read_disk_free_gb", return_value=100.0),
                patch("teatree.loop.scanners.resource_pressure.read_ram_avail_gb", return_value=1.0),
                patch(
                    "teatree.core.models.resource_pressure_marker.ResourcePressureMarker.load",
                    return_value=marker,
                ),
            ):
                signals = scanner.scan()
        assert any(s.kind == "resource.cleanup_needed" for s in signals)


class MarkerStrTests(TestCase):
    def test_str_includes_resource_figures(self) -> None:
        marker = ResourcePressureMarker.load()
        marker.last_disk_free_gb = 12.5
        marker.last_ram_avail_gb = 3.2
        assert "12.5gb" in str(marker)
        assert "3.2gb" in str(marker)


class ScannerNameTests(TestCase):
    def test_name_is_resource_pressure(self) -> None:
        assert ResourcePressureScanner().name == "resource_pressure"
