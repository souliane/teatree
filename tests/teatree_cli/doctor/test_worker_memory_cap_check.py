"""``_check_worker_memory_cap`` — the `t3 doctor` under-sized-worker-memory guard.

ROLE-AWARE: only the ``worker`` container runs headless agents plus their
commit/``ty-check``/lint hooks, and a too-low ``mem_limit`` there OOM-kills them
(exit 137) even on an idle host. The lean admin (Django web UI) and slack-listener
are meant to be small, so this must NEVER warn for them — it fires only when doctor
runs inside the worker and that container's own cgroup cap is under the floor. It is
surfacing-only (always returns ``True``).

Role + cgroup files are stubbed so every branch is exercised deterministically,
independent of the real container.
"""

from pathlib import Path

from teatree.cli.doctor.checks_resources import (
    _BYTES_PER_GIB,
    _check_worker_memory_cap,
    _read_cgroup_memory_cap,
    _worker_floor_bytes,
)


def _cgroup(tmp_path: Path, *, v2: str | None = None, v1: str | None = None) -> tuple[Path, Path]:
    v2_path = tmp_path / "memory.max"
    v1_path = tmp_path / "memory.limit_in_bytes"
    if v2 is not None:
        v2_path.write_text(v2, encoding="utf-8")
    if v1 is not None:
        v1_path.write_text(v1, encoding="utf-8")
    return v2_path, v1_path


class TestReadCgroupMemoryCap:
    def test_reads_v2_bytes(self, tmp_path: Path) -> None:
        v2, v1 = _cgroup(tmp_path, v2=str(6 * _BYTES_PER_GIB))
        assert _read_cgroup_memory_cap(v2, v1) == 6 * _BYTES_PER_GIB

    def test_v2_max_is_uncapped(self, tmp_path: Path) -> None:
        v2, v1 = _cgroup(tmp_path, v2="max")
        assert _read_cgroup_memory_cap(v2, v1) is None

    def test_falls_back_to_v1_when_v2_absent(self, tmp_path: Path) -> None:
        v2, v1 = _cgroup(tmp_path, v1=str(512 * 1024 * 1024))
        assert _read_cgroup_memory_cap(v2, v1) == 512 * 1024 * 1024

    def test_v1_unlimited_sentinel_is_uncapped(self, tmp_path: Path) -> None:
        v2, v1 = _cgroup(tmp_path, v1="9223372036854771712")
        assert _read_cgroup_memory_cap(v2, v1) is None

    def test_absent_files_is_uncapped(self, tmp_path: Path) -> None:
        assert _read_cgroup_memory_cap(tmp_path / "nope-v2", tmp_path / "nope-v1") is None


class TestWorkerFloorBytes:
    def test_default_when_unset(self) -> None:
        assert _worker_floor_bytes(None) == 4 * _BYTES_PER_GIB

    def test_parses_a_valid_override(self) -> None:
        assert _worker_floor_bytes("8") == 8 * _BYTES_PER_GIB

    def test_garbage_and_nonpositive_fall_back(self) -> None:
        assert _worker_floor_bytes("nope") == 4 * _BYTES_PER_GIB
        assert _worker_floor_bytes("0") == 4 * _BYTES_PER_GIB


class TestWorkerMemoryCapCheck:
    def test_worker_under_floor_hard_fails(self, tmp_path: Path, capsys) -> None:
        v2, v1 = _cgroup(tmp_path, v2=str(512 * 1024 * 1024))  # a grossly under-sized worker
        # Critical, not advisory: an OOM-prone worker gates the doctor exit code.
        assert _check_worker_memory_cap(role="worker", v2=v2, v1=v1) is False
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "worker container memory cap" in out
        assert "TEATREE_WORKER_MEM_LIMIT" in out

    def test_worker_above_floor_is_silent(self, tmp_path: Path, capsys) -> None:
        v2, v1 = _cgroup(tmp_path, v2=str(18 * _BYTES_PER_GIB))  # the shipped worker default
        assert _check_worker_memory_cap(role="worker", v2=v2, v1=v1) is True
        assert capsys.readouterr().out == ""

    def test_lean_admin_cap_never_warns(self, tmp_path: Path, capsys) -> None:
        # A non-worker role must NOT warn even when its cap sits under the worker floor.
        v2, v1 = _cgroup(tmp_path, v2=str(512 * 1024 * 1024))
        assert _check_worker_memory_cap(role="admin", v2=v2, v1=v1) is True
        assert capsys.readouterr().out == ""

    def test_no_role_is_silent(self, tmp_path: Path, capsys) -> None:
        # A host / roleless invocation (TEATREE_ROLE unset) is not the worker — skip.
        v2, v1 = _cgroup(tmp_path, v2=str(512 * 1024 * 1024))
        assert _check_worker_memory_cap(role="", v2=v2, v1=v1) is True
        assert capsys.readouterr().out == ""

    def test_worker_uncapped_is_silent(self, tmp_path: Path, capsys) -> None:
        v2, v1 = _cgroup(tmp_path, v2="max")
        assert _check_worker_memory_cap(role="worker", v2=v2, v1=v1) is True
        assert capsys.readouterr().out == ""

    def test_floor_override_is_honored(self, tmp_path: Path, capsys, monkeypatch) -> None:
        monkeypatch.setenv("TEATREE_WORKER_MEMORY_FLOOR_GIB", "24")
        v2, v1 = _cgroup(tmp_path, v2=str(18 * _BYTES_PER_GIB))  # 18g now below a 24g floor
        assert _check_worker_memory_cap(role="worker", v2=v2, v1=v1) is False
        assert "FAIL" in capsys.readouterr().out
