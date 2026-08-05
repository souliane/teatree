"""Scope-qualified cgroup memory readings (#4217).

The defect these pin is not arithmetic. ``tests/teatree_utils/test_ram_probe.py`` tested
the old unscoped reader with the scope-determining input patched out, so it was green for
every value of the thing that was wrong. Here the **cgroup identity** is the variable: the
same arithmetic, run in a 2 GiB sidecar and in a worker-sized cgroup, must produce answers
the box-wide watermarks treat differently.

The cgroup fake is keyed on FILENAME rather than on call order, so a test states which
container it is standing in and changes nothing else.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils.ram_scope import (
    AGENT_WORKLOAD_FLOOR_ENV,
    DEFAULT_AGENT_WORKLOAD_FLOOR_GIB,
    RamHeadroom,
    agent_workload_floor_gib,
    cgroup_headroom_mib,
    cgroup_v2_reclaimable_mib,
    read_ram_headroom,
)

_MIB = 1024 * 1024
_GIB = 1024 * _MIB

# The two readings the issue records, taken at the same instant on the same box.
_ADMIN_LIMIT_BYTES = 2 * _GIB
_ADMIN_CURRENT_BYTES = 357 * _MIB
_WORKER_LIMIT_BYTES = 23231201280
_WORKER_CURRENT_BYTES = 5916573696
_WORKER_INACTIVE_FILE_BYTES = 832724992
_WORKER_SLAB_RECLAIMABLE_BYTES = 268435456
_HOST_AVAILABLE_MIB = 22557


def _memory_stat(*, inactive_file: int, slab_reclaimable: int) -> str:
    """A ``memory.stat`` body — the real file's shape, with the irrelevant keys kept.

    ``file`` is deliberately larger than ``inactive_file``: crediting the whole page cache
    would overstate headroom, so a reader that summed it would come out too high here.
    """
    return (
        f"anon 4194304000\n"
        f"file {inactive_file * 2}\n"
        f"kernel_stack 1048576\n"
        f"slab_reclaimable {slab_reclaimable}\n"
        f"slab_unreclaimable 33554432\n"
        f"inactive_anon 4194304000\n"
        f"inactive_file {inactive_file}\n"
        f"active_file {inactive_file}\n"
    )


@contextmanager
def _in_cgroup(
    *,
    limit_bytes: int | None,
    current_bytes: int | None,
    inactive_file: int = 0,
    slab_reclaimable: int = 0,
    host_available_mib: int = _HOST_AVAILABLE_MIB,
) -> Iterator[None]:
    """Stand the probe inside one specific cgroup, with the host reading held fixed.

    ``limit_bytes=None`` is the uncapped host (no ``memory.max``/``memory.current`` at all).
    """
    files: dict[str, str] = {}
    if limit_bytes is not None:
        files["memory.max"] = f"{limit_bytes}\n"
    if current_bytes is not None:
        files["memory.current"] = f"{current_bytes}\n"
        files["memory.stat"] = _memory_stat(inactive_file=inactive_file, slab_reclaimable=slab_reclaimable)
    with (
        patch("pathlib.Path.read_text", _cgroup_read_text(files)),
        patch("teatree.utils.ram_scope.host_available_ram_mib", return_value=host_available_mib),
    ):
        yield


def _cgroup_read_text(files: dict[str, str]) -> Callable[..., str]:
    """A ``Path.read_text`` replacement keyed on the file NAME, not on call order."""

    def read_text(self: Path, *_args: object, **_kwargs: object) -> str:
        if self.name not in files:
            raise FileNotFoundError(self.name)
        return files[self.name]

    return read_text


class TestTheReadingCarriesItsScope:
    """A number the absolute watermarks may judge, or UNKNOWN — never a sidecar's number."""

    def test_a_sidecar_cgroup_is_unknown_to_box_wide_watermarks(self) -> None:
        # The live admin container: a fixed 2 GiB cap, 357 MiB charged, on a box with
        # 22 GB free. The old reader answered 1.65 GB and every dispatch was denied.
        with _in_cgroup(limit_bytes=_ADMIN_LIMIT_BYTES, current_bytes=_ADMIN_CURRENT_BYTES):
            headroom = read_ram_headroom()
        assert headroom.available_mib == 1691
        assert headroom.cgroup_limit_mib == 2048
        assert headroom.box_watermark_mib is None

    def test_a_worker_sized_cgroup_still_produces_a_number(self) -> None:
        with _in_cgroup(
            limit_bytes=_WORKER_LIMIT_BYTES,
            current_bytes=_WORKER_CURRENT_BYTES,
            inactive_file=_WORKER_INACTIVE_FILE_BYTES,
            slab_reclaimable=_WORKER_SLAB_RECLAIMABLE_BYTES,
        ):
            headroom = read_ram_headroom()
        assert headroom.available_mib is not None
        assert headroom.box_watermark_mib == headroom.available_mib

    def test_a_worker_sized_cgroup_that_is_genuinely_low_still_reports_low(self) -> None:
        # Scope-qualifying must not launder a REAL shortage into UNKNOWN: same worker cap,
        # nearly all of it genuinely charged as anon.
        with _in_cgroup(
            limit_bytes=_WORKER_LIMIT_BYTES,
            current_bytes=_WORKER_LIMIT_BYTES - 512 * _MIB,
            host_available_mib=_HOST_AVAILABLE_MIB,
        ):
            headroom = read_ram_headroom()
        assert headroom.box_watermark_mib == 512

    def test_an_uncapped_host_is_always_judgeable(self) -> None:
        with _in_cgroup(limit_bytes=None, current_bytes=None):
            headroom = read_ram_headroom()
        assert headroom.cgroup_limit_mib is None
        assert headroom.box_watermark_mib == _HOST_AVAILABLE_MIB

    def test_nothing_readable_stays_unknown_not_zero(self) -> None:
        with _in_cgroup(limit_bytes=None, current_bytes=None, host_available_mib=0):
            headroom = read_ram_headroom()
        assert headroom.available_mib is None
        assert headroom.box_watermark_mib is None

    def test_a_cgroup_exactly_at_the_floor_is_judgeable(self) -> None:
        # The floor is the smallest cgroup that can host an agent workload, so AT it the
        # reading still describes something the watermarks were written for.
        floor_bytes = DEFAULT_AGENT_WORKLOAD_FLOOR_GIB * _GIB
        with _in_cgroup(limit_bytes=floor_bytes, current_bytes=1 * _GIB):
            assert read_ram_headroom().box_watermark_mib is not None

    def test_the_floor_override_moves_the_scope_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(AGENT_WORKLOAD_FLOOR_ENV, "32")
        with _in_cgroup(limit_bytes=_WORKER_LIMIT_BYTES, current_bytes=_WORKER_CURRENT_BYTES):
            assert read_ram_headroom().box_watermark_mib is None

    def test_a_reading_with_no_cap_recorded_is_judged(self) -> None:
        assert RamHeadroom(available_mib=1691, cgroup_limit_mib=None).box_watermark_mib == 1691


class TestReclaimableCacheIsCredited:
    """``memory.current`` charges page cache the kernel hands straight back."""

    def test_the_correction_changes_the_answer_on_a_real_memory_stat(self) -> None:
        with _in_cgroup(
            limit_bytes=_WORKER_LIMIT_BYTES,
            current_bytes=_WORKER_CURRENT_BYTES,
            inactive_file=_WORKER_INACTIVE_FILE_BYTES,
            slab_reclaimable=_WORKER_SLAB_RECLAIMABLE_BYTES,
        ):
            corrected = cgroup_headroom_mib()
        uncorrected = _WORKER_LIMIT_BYTES // _MIB - _WORKER_CURRENT_BYTES // _MIB
        reclaimable = (_WORKER_INACTIVE_FILE_BYTES + _WORKER_SLAB_RECLAIMABLE_BYTES) // _MIB
        assert corrected == uncorrected + reclaimable
        assert corrected > uncorrected

    def test_only_the_reclaimable_keys_are_credited(self) -> None:
        # `file` (2x inactive_file here) and `anon` must not be summed — active file pages
        # come back only under real pressure, and anon never does.
        with _in_cgroup(
            limit_bytes=_WORKER_LIMIT_BYTES,
            current_bytes=_WORKER_CURRENT_BYTES,
            inactive_file=_WORKER_INACTIVE_FILE_BYTES,
            slab_reclaimable=_WORKER_SLAB_RECLAIMABLE_BYTES,
        ):
            assert cgroup_v2_reclaimable_mib() == (_WORKER_INACTIVE_FILE_BYTES + _WORKER_SLAB_RECLAIMABLE_BYTES) // _MIB

    def test_a_cache_saturated_cgroup_is_not_reported_full(self) -> None:
        # The trend the issue names: after a day of `-n auto` suites `memory.current` sits
        # near `memory.max` and is almost all page cache.
        cache = 20 * _GIB
        with _in_cgroup(
            limit_bytes=_WORKER_LIMIT_BYTES,
            current_bytes=_WORKER_LIMIT_BYTES - 256 * _MIB,
            inactive_file=cache,
            slab_reclaimable=0,
        ):
            headroom = cgroup_headroom_mib()
        assert headroom is not None
        assert headroom > cache // _MIB

    def test_an_unreadable_memory_stat_credits_nothing(self) -> None:
        with patch("pathlib.Path.read_text", _cgroup_read_text({})):
            assert cgroup_v2_reclaimable_mib() == 0

    def test_a_garbled_memory_stat_credits_nothing(self) -> None:
        garbled = {"memory.stat": "inactive_file nope\nslab_reclaimable\n"}
        with patch("pathlib.Path.read_text", _cgroup_read_text(garbled)):
            assert cgroup_v2_reclaimable_mib() == 0

    def test_headroom_never_exceeds_the_cap(self) -> None:
        # A reclaimable figure larger than what is charged must not manufacture headroom.
        with _in_cgroup(limit_bytes=8 * _GIB, current_bytes=1 * _GIB, inactive_file=4 * _GIB):
            assert cgroup_headroom_mib() == 8 * 1024

    def test_an_uncapped_cgroup_has_no_headroom_figure(self) -> None:
        with patch("pathlib.Path.read_text", _cgroup_read_text({})):
            assert cgroup_headroom_mib() is None


class TestAgentWorkloadFloor:
    """One floor, shared with ``t3 doctor``'s worker-cap FAIL."""

    def test_absent_override_is_the_default(self) -> None:
        assert agent_workload_floor_gib(None) == DEFAULT_AGENT_WORKLOAD_FLOOR_GIB

    def test_a_valid_override_wins(self) -> None:
        assert agent_workload_floor_gib("8") == 8

    def test_garbage_and_nonpositive_fall_back(self) -> None:
        assert agent_workload_floor_gib("nope") == DEFAULT_AGENT_WORKLOAD_FLOOR_GIB
        assert agent_workload_floor_gib("0") == DEFAULT_AGENT_WORKLOAD_FLOOR_GIB
        assert agent_workload_floor_gib("-4") == DEFAULT_AGENT_WORKLOAD_FLOOR_GIB

    def test_the_env_name_is_the_one_the_doctor_check_documents(self) -> None:
        assert AGENT_WORKLOAD_FLOOR_ENV == "TEATREE_WORKER_MEMORY_FLOOR_GIB"


def test_this_host_can_answer() -> None:
    """A probe that cannot read the host it runs on is the defect, not the fixture."""
    assert read_ram_headroom().available_mib is not None
