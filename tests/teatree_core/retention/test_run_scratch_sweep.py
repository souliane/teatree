import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.core.retention.run_scratch_sweep import RUN_SCRATCH_MAX_AGE_SECONDS, sweep_run_scratch

_OLD = time.time() - 4 * 60 * 60
_AGE_MARGIN_SECONDS = 120


def _aged(root: Path, name: str, *, payload: bytes = b"data", mtime: float = _OLD) -> Path:
    target = root / name
    target.mkdir()
    file = target / "payload"
    file.write_bytes(payload)
    os.utime(file, (mtime, mtime))
    os.utime(target, (mtime, mtime))
    return target


def _pid(proc_root: Path, name: str, *, cwd: Path, cmdline: bytes = b"sleep\x001\x00", environ: bytes = b"") -> None:
    pid = proc_root / name
    pid.mkdir()
    (pid / "cwd").symlink_to(cwd)
    (pid / "cmdline").write_bytes(cmdline)
    (pid / "environ").write_bytes(environ)


def _unused_candidate(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "scratch"
    root.mkdir()
    candidate = _aged(root, "t3-merge-tree-abcdefgh")
    proc = tmp_path / "proc"
    proc.mkdir()
    _pid(proc, "123", cwd=tmp_path)
    return root, candidate, proc


def test_sweep_removes_only_aged_unused_named_families(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    _pid(proc, "123", cwd=tmp_path)
    verify = _aged(root, "t3-verify-gates-abcdefgh", payload=b"12345")
    merge = _aged(root, "t3-merge-tree-abcdefgh", payload=b"123")
    recent = root / "t3-verify-gates-ijklmnop"
    recent.mkdir()
    _aged(root, "t3-review-abcdefgh")
    _aged(root, "t3-live-abcdefgh")
    _aged(root, "t3-verify-gates-bad-suffix")
    (root / "t3-merge-tree-ijklmnop").symlink_to(merge, target_is_directory=True)
    (root / "t3-verify-gates-qrstuvwx").write_bytes(b"file")

    result = sweep_run_scratch(root, proc_root=proc)

    assert result.freed_bytes == 8
    assert not verify.exists()
    assert not merge.exists()
    assert recent.is_dir()
    assert (root / "t3-review-abcdefgh").is_dir()
    assert (root / "t3-live-abcdefgh").is_dir()
    assert (root / "t3-verify-gates-bad-suffix").is_dir()
    assert (root / "t3-merge-tree-ijklmnop").is_symlink()
    assert (root / "t3-verify-gates-qrstuvwx").is_file()


def test_sweep_separates_just_idle_enough_from_just_too_recent(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    _pid(proc, "123", cwd=tmp_path)
    now = time.time()
    too_recent = _aged(root, "t3-verify-gates-abcdefgh", mtime=now - RUN_SCRATCH_MAX_AGE_SECONDS + _AGE_MARGIN_SECONDS)
    idle_enough = _aged(root, "t3-merge-tree-abcdefgh", mtime=now - RUN_SCRATCH_MAX_AGE_SECONDS - _AGE_MARGIN_SECONDS)

    result = sweep_run_scratch(root, proc_root=proc)

    assert result.refusal == ""
    assert too_recent.is_dir()
    assert not idle_enough.exists()


def test_sweep_keeps_a_directory_a_reviewer_touched_an_hour_ago(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    _pid(proc, "123", cwd=tmp_path)
    touched_an_hour_ago = _aged(root, "t3-verify-gates-abcdefgh", mtime=time.time() - 60 * 60)
    abandoned = _aged(root, "t3-merge-tree-abcdefgh")

    result = sweep_run_scratch(root, proc_root=proc)

    assert result.refusal == ""
    assert touched_an_hour_ago.is_dir()
    assert not abandoned.exists()


def test_sweep_keeps_a_directory_whose_process_works_in_a_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    held = _aged(root, "t3-verify-gates-abcdefgh")
    nested = held / "pkg" / "deep"
    nested.mkdir(parents=True)
    for aged in (nested, nested.parent, held):
        os.utime(aged, (_OLD, _OLD))
    unheld = _aged(root, "t3-merge-tree-abcdefgh")
    _pid(proc, "123", cwd=nested)

    result = sweep_run_scratch(root, proc_root=proc)

    assert result.refusal == ""
    assert held.is_dir()
    assert not unheld.exists()


def test_aged_symlink_and_plain_file_named_like_a_family_are_never_touched(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    _pid(proc, "123", cwd=tmp_path)
    target = _aged(tmp_path, "elsewhere")
    link = root / "t3-merge-tree-abcdefgh"
    link.symlink_to(target, target_is_directory=True)
    os.utime(link, (_OLD, _OLD), follow_symlinks=False)
    plain = root / "t3-verify-gates-abcdefgh"
    plain.write_bytes(b"file")
    os.utime(plain, (_OLD, _OLD))
    control = _aged(root, "t3-merge-tree-ijklmnop")

    result = sweep_run_scratch(root, proc_root=proc)

    assert (result.refusal, result.removed) == ("", 1)
    assert not control.exists()
    assert link.is_symlink()
    assert (target / "payload").is_file()
    assert plain.is_file()
    assert "could not remove run scratch" not in caplog.text


@pytest.mark.parametrize("holder", ["cwd", "cmdline", "environ"])
def test_sweep_keeps_a_directory_in_use(tmp_path: Path, holder: str) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    candidate = _aged(root, "t3-verify-gates-abcdefgh")
    proc = tmp_path / "proc"
    proc.mkdir()
    _pid(
        proc,
        "123",
        cwd=candidate if holder == "cwd" else tmp_path,
        cmdline=str(candidate).encode() + b"\x00" if holder == "cmdline" else b"sleep\x001\x00",
        environ=b"X=" + str(candidate).encode() + b"/control-db\x00" if holder == "environ" else b"",
    )

    result = sweep_run_scratch(root, proc_root=proc)

    assert result.freed_bytes == 0
    assert candidate.is_dir()


def test_sweep_uses_newest_nested_mtime(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    candidate = _aged(root, "t3-merge-tree-abcdefgh")
    (candidate / "payload").write_bytes(b"recent")
    proc = tmp_path / "proc"
    proc.mkdir()
    _pid(proc, "123", cwd=tmp_path)

    assert sweep_run_scratch(root, proc_root=proc).freed_bytes == 0
    assert candidate.is_dir()


def test_unreadable_same_uid_process_refuses_every_deletion(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    candidate = _aged(root, "t3-merge-tree-abcdefgh")
    proc = tmp_path / "proc"
    proc.mkdir()
    _pid(proc, "123", cwd=tmp_path)

    with patch("pathlib.Path.read_bytes", side_effect=PermissionError("denied")):
        result = sweep_run_scratch(root, proc_root=proc)

    assert result.refusal
    assert result.freed_bytes == 0
    assert candidate.is_dir()


@pytest.mark.parametrize("names_candidate", [False, True])
def test_process_with_unreadable_cwd_and_environ_is_judged_by_cmdline(tmp_path: Path, *, names_candidate: bool) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    cmdline = os.fsencode(candidate) + b"\0" if names_candidate else b"gpg-agent\0"
    _pid(proc, "39", cwd=tmp_path, cmdline=cmdline)
    original_readlink, original_read = Path.readlink, Path.read_bytes

    def guarded_readlink(path: Path) -> Path:
        if path.parent.name == "39":
            raise PermissionError
        return original_readlink(path)

    def guarded_read(path: Path) -> bytes:
        if path.parent.name == "39" and path.name == "environ":
            raise PermissionError
        return original_read(path)

    with patch("pathlib.Path.readlink", new=guarded_readlink), patch("pathlib.Path.read_bytes", new=guarded_read):
        result = sweep_run_scratch(root, proc_root=proc)

    assert not result.refusal
    assert candidate.exists() is names_candidate


def test_missing_root_is_empty_and_unlistable_root_refuses(tmp_path: Path) -> None:
    assert sweep_run_scratch(tmp_path / "absent").freed_bytes == 0
    with patch("pathlib.Path.iterdir", side_effect=PermissionError("denied")):
        result = sweep_run_scratch(tmp_path)
    assert "scratch root unreadable" in result.refusal


def test_unreadable_or_empty_process_table_refuses(tmp_path: Path) -> None:
    root, candidate, _proc = _unused_candidate(tmp_path)
    empty_proc = tmp_path / "empty-proc"
    empty_proc.mkdir()
    assert "no numeric pids" in sweep_run_scratch(root, proc_root=empty_proc).refusal
    assert "process table unreadable" in sweep_run_scratch(root, proc_root=tmp_path / "missing").refusal
    assert candidate.is_dir()


def test_same_uid_process_with_missing_cwd_refuses(tmp_path: Path) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    (proc / "123" / "cwd").unlink()

    result = sweep_run_scratch(root, proc_root=proc)

    assert "unreadable process reference" in result.refusal
    assert candidate.is_dir()


def test_process_that_vanishes_during_probe_does_not_block(tmp_path: Path) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    _pid(proc, "124", cwd=tmp_path)
    (proc / "124" / "cwd").unlink()
    original_exists = Path.exists

    def vanished(path: Path, *args: object, **kwargs: object) -> bool:
        return False if path == proc / "124" else original_exists(path, *args, **kwargs)

    with patch("pathlib.Path.exists", new=vanished):
        result = sweep_run_scratch(root, proc_root=proc)

    assert result.freed_bytes == 4
    assert not candidate.exists()


def test_foreign_uid_process_does_not_blind_owned_candidate(tmp_path: Path) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    _pid(proc, "124", cwd=tmp_path)
    (proc / "124" / "cwd").unlink()
    original_stat = Path.stat

    def foreign(path: Path, *args: object, **kwargs: object) -> os.stat_result | SimpleNamespace:
        if path == proc / "124":
            return SimpleNamespace(st_uid=os.geteuid() + 1)
        return original_stat(path, *args, **kwargs)

    with patch("pathlib.Path.stat", new=foreign):
        result = sweep_run_scratch(root, proc_root=proc)

    assert result.freed_bytes == 4
    assert not candidate.exists()


def test_unstatable_candidate_is_kept(tmp_path: Path) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    original_stat = Path.stat
    calls = 0

    def disappearing(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        nonlocal calls
        if path == candidate:
            calls += 1
            if calls == 3:
                raise OSError
        return original_stat(path, *args, **kwargs)

    with patch("pathlib.Path.stat", new=disappearing):
        result = sweep_run_scratch(root, proc_root=proc)

    assert result.freed_bytes == 0
    assert candidate.is_dir()


def test_other_uid_candidate_is_kept(tmp_path: Path) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    original_stat = Path.stat

    def other_owner(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        info = original_stat(path, *args, **kwargs)
        if path == candidate:
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    with patch("pathlib.Path.stat", new=other_owner):
        result = sweep_run_scratch(root, proc_root=proc)

    assert result.freed_bytes == 0
    assert candidate.is_dir()


@pytest.mark.parametrize("failure", [OSError("busy"), None])
def test_failed_or_unconfirmed_removal_is_not_counted(
    tmp_path: Path, failure: OSError | None, caplog: pytest.LogCaptureFixture
) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    with patch("teatree.utils.run_scratch.shutil.rmtree", side_effect=failure):
        result = sweep_run_scratch(root, proc_root=proc)

    assert result.freed_bytes == 0
    assert result.removed == 0
    assert candidate.is_dir()
    assert ("could not remove" in caplog.text) is (failure is not None)


def test_sweep_removes_a_read_only_subtree(tmp_path: Path) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    (candidate / "pkg").mkdir()
    (candidate / "pkg" / "payload").write_bytes(b"data")
    for aged in (candidate / "pkg" / "payload", candidate / "pkg", candidate):
        os.utime(aged, (_OLD, _OLD))
    (candidate / "pkg").chmod(0o555)

    result = sweep_run_scratch(root, proc_root=proc)

    assert result.removed == 1
    assert not candidate.exists()


def test_zombie_holds_nothing_and_does_not_refuse_the_pass(tmp_path: Path) -> None:
    root, candidate, _proc = _unused_candidate(tmp_path)
    zombie = subprocess.Popen([sys.executable, "-c", ""])
    stat = Path("/proc") / str(zombie.pid) / "stat"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and stat.read_text().rpartition(")")[2].split()[0] != "Z":
        time.sleep(0.01)
    original_iterdir = Path.iterdir

    def only_zombie_in_proc(path: Path) -> Iterator[Path]:
        if path == Path("/proc"):
            return iter((Path("/proc") / str(zombie.pid),))
        return original_iterdir(path)

    try:
        with patch("pathlib.Path.iterdir", new=only_zombie_in_proc):
            result = sweep_run_scratch(root)
    finally:
        zombie.wait(timeout=5)

    assert result.refusal == ""
    assert not candidate.exists()


def test_deadline_or_recheck_refusal_keeps_candidate(tmp_path: Path) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    timed_out = sweep_run_scratch(root, proc_root=proc, deadline=time.monotonic() - 1)
    original_read = Path.read_bytes
    reads = 0

    def refused_on_recheck(path: Path) -> bytes:
        nonlocal reads
        reads += 1
        if reads > 2:
            raise PermissionError
        return original_read(path)

    with patch("pathlib.Path.read_bytes", new=refused_on_recheck):
        rechecked = sweep_run_scratch(root, proc_root=proc)

    assert timed_out.refusal == "pass budget exhausted"
    assert "unreadable process reference" in rechecked.refusal
    assert candidate.is_dir()


def test_new_reference_between_scans_keeps_candidate(tmp_path: Path) -> None:
    root, candidate, proc = _unused_candidate(tmp_path)
    original_readlink = Path.readlink
    reads = 0

    def gained_cwd(path: Path) -> Path:
        nonlocal reads
        if path == proc / "123" / "cwd":
            reads += 1
            return candidate if reads > 1 else tmp_path
        return original_readlink(path)

    with patch("pathlib.Path.readlink", new=gained_cwd):
        result = sweep_run_scratch(root, proc_root=proc)

    assert result.freed_bytes == 0
    assert candidate.is_dir()


def test_real_process_cwd_protects_then_releases_directory(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    candidate = _aged(root, "t3-merge-tree-abcdefgh")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=candidate)
    original_iterdir = Path.iterdir

    def only_child_in_proc(path: Path) -> Iterator[Path]:
        if path == Path("/proc"):
            return iter((Path("/proc") / str(child.pid),))
        return original_iterdir(path)

    try:
        with patch("pathlib.Path.iterdir", new=only_child_in_proc):
            assert (Path("/proc") / str(child.pid) / "cwd").readlink() == candidate
            first = sweep_run_scratch(root)
            assert first.refusal == ""
            assert first.freed_bytes == 0
            assert candidate.is_dir()
            child.terminate()
            child.wait(timeout=5)
            second = sweep_run_scratch(root)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
    assert second.refusal == ""
    assert second.freed_bytes == 4
    assert not candidate.exists()
