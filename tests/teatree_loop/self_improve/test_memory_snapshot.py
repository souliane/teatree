"""Memory and swap repair tickets retain the box state observed at firing."""

import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase

from teatree.core.models import SelfImproveFiring, Ticket
from teatree.loop.self_improve import ActionRung, DetectorReport, actions, memory_snapshot, run_action_ladder
from teatree.utils.disposable_checkout import DISPOSABLE_ROOTS_ENV
from teatree.utils.ram_probe import host_available_ram_mib as actual_host_available_ram_mib
from teatree.utils.ram_scope import read_ram_headroom as actual_read_ram_headroom
from teatree.utils.singleton import pid_alive as actual_pid_alive

_MIB = 1024 * 1024
_OWN_NAMESPACE = "pid:[111]"


def stage_process(
    root: Path,
    pid: int,
    *,
    ppid: int,
    rss_pages: int,
    comm: str,
) -> None:
    process = root / str(pid)
    process.mkdir(parents=True)
    fields = ["0"] * 20
    fields[0] = "S"
    fields[1] = str(ppid)
    fields[2] = str(pid)
    fields[19] = "10000"
    (process / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
    (process / "statm").write_text(f"10000 {rss_pages} 0\n")
    (process / "status").write_text(f"NSpid:\t{pid}\t{pid}\n")


def stage_pid_namespace(root: Path, pid: int, namespace: str) -> None:
    (root / str(pid) / "ns").mkdir()
    (root / str(pid) / "ns" / "pid").symlink_to(namespace)


def stage_worker_tree(root: Path) -> None:
    stage_process(root, 10, ppid=1, rss_pages=256, comm="worker")
    stage_pid_namespace(root, 10, _OWN_NAMESPACE)
    (root / "10" / "status").write_text("NSpid:\t10\t7\n")
    stage_process(root, 11, ppid=10, rss_pages=1024, comm="python")


def process_rows(context: str) -> dict[str, str]:
    return {line.split()[0]: line for line in context.splitlines() if line.startswith("pid=")}


@contextmanager
def staged_box(base: Path) -> Generator[Path]:
    proc = base / "proc"
    proc.mkdir()
    (proc / "uptime").write_text("1000.0 200.0\n")
    cgroup = base / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.max").write_text(str(16 * 1024 * _MIB))
    (cgroup / "memory.current").write_text(str(13 * 1024 * _MIB))
    (cgroup / "memory.stat").write_text(
        f"anon {7 * 1024 * _MIB}\nshmem {_MIB}\nactive_file {2 * 1024 * _MIB}\n"
        f"inactive_file {1024 * _MIB}\nslab_reclaimable 0\n"
    )
    meminfo = base / "meminfo"
    meminfo.write_text("MemAvailable: 12582912 kB\n")
    root = base / "worktrees"
    root.mkdir()
    worker_file = base / "worker.pid"
    worker_file.write_text(
        "7\n" + json.dumps({"pid_namespace": _OWN_NAMESPACE, "hostname": "box", "role": "worker"}) + "\n"
    )
    with (
        patch.object(memory_snapshot, "host_proc_root", return_value=(proc, "")),
        patch.object(memory_snapshot, "discover_overlays", return_value=[SimpleNamespace(name="acme")]),
        patch.object(memory_snapshot, "worktree_root", return_value=root),
        patch.object(memory_snapshot, "default_pid_path", return_value=worker_file),
        patch("teatree.utils.singleton.pid_alive", return_value=True),
        patch.object(memory_snapshot, "current_pid_namespace", return_value=_OWN_NAMESPACE),
        patch("teatree.core.loop_lease_liveness.reader_pid_namespace", return_value=_OWN_NAMESPACE),
        patch("teatree.utils.ram_scope.cgroup_file", side_effect=lambda name, **_kwargs: cgroup / name),
        patch("teatree.utils.ram_probe.cgroup_file", side_effect=lambda name, **_kwargs: cgroup / name),
        patch("teatree.utils.ram_scope.host_available_ram_mib", actual_host_available_ram_mib),
        patch("teatree.utils.ram_scope.read_ram_headroom", actual_read_ram_headroom),
        patch("teatree.utils.ram_probe._MEMINFO_PATH", str(meminfo)),
    ):
        yield proc


def report(cause: str, *, state_hash: str = "first") -> DetectorReport:
    return DetectorReport(
        detector="pressure_incident",
        dedup_key=f"pressure_incident::{cause}",
        state_hash=state_hash,
        severity="critical",
        max_rung=ActionRung.TICKET,
        summary="staged pressure",
        payload={"cause": cause, "suggested_action": "Inspect the box"},
        requested_rung=ActionRung.TICKET,
    )


class MemorySnapshotTicketTests(TestCase):
    def test_memory_ticket_carries_scopes_and_ranked_processes(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            root = Path(directory) / "worktrees"
            stage_worker_tree(proc)
            (proc / "11" / "cwd").symlink_to(root / "ticket-5321" / "repo")
            stage_process(proc, 20, ppid=1, rss_pages=768, comm="other")
            stage_process(proc, 30, ppid=1, rss_pages=128, comm="claude")
            stage_process(proc, 31, ppid=30, rss_pages=512, comm="shell")
            stage_process(proc, 40, ppid=1, rss_pages=128, comm="2.1.300")
            stage_process(proc, 41, ppid=40, rss_pages=256, comm="shell")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Judged RAM: 4096 MiB (cgroup bound)" in context, context
        assert "Host MemAvailable: 12288 MiB" in context
        assert "Cgroup cap: 16384 MiB; headroom: 4096 MiB; box-scoped: yes" in context
        assert "anon=7168 MiB" in context
        assert "shmem=1 MiB" in context
        assert "active_file=2048 MiB" in context
        assert "inactive_file=1024 MiB" in context
        assert "slab_reclaimable=0 MiB" in context
        assert context.index("pid=11 ") < context.index("pid=20 ") < context.index("pid=31 ")
        assert "pid=11 ppid=10" in context
        assert "ticket-dir=ticket-5321 attached" in context
        assert "pid=20 ppid=1" in context
        assert "ticket-dir=unreadable (FileNotFoundError) detached" in context
        assert "pid=31 ppid=30" in context
        assert "pid=41 ppid=40" in context
        rows = process_rows(context)
        assert rows["pid=31"].endswith(" attached")
        assert rows["pid=41"].endswith(" attached")
        assert "attached = ancestry reaches the worker or an agent CLI" in context
        assert str(Path(directory)) not in context

    def test_process_numbered_like_the_worker_in_another_pid_namespace_is_not_the_worker(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 50, ppid=1, rss_pages=256, comm="sidecar")
            stage_pid_namespace(proc, 50, "pid:[1]")
            (proc / "50" / "status").write_text("NSpid:\t50\t7\n")
            stage_process(proc, 51, ppid=50, rss_pages=1024, comm="python")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        rows = process_rows(context)
        assert rows["pid=51"].endswith(" detached")

    def test_unknown_own_pid_namespace_never_matches_a_worker(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 10, ppid=1, rss_pages=256, comm="worker")
            (proc / "10" / "status").write_text("NSpid:\t10\t7\n")
            stage_process(proc, 11, ppid=10, rss_pages=1024, comm="python")
            with (
                patch.object(memory_snapshot, "current_pid_namespace", return_value=""),
                patch("teatree.core.loop_lease_liveness.reader_pid_namespace", return_value=""),
            ):
                result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        rows = process_rows(context)
        assert rows["pid=11"].endswith(" detached")

    def test_foreign_holder_namespace_never_attributes_matching_pid(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            worker_file = Path(directory) / "worker.pid"
            worker_file.write_text('7\n{"pid_namespace": "pid:[999]", "hostname": "other", "role": "worker"}\n')
            stage_process(proc, 7, ppid=1, rss_pages=256, comm="worker")
            stage_pid_namespace(proc, 7, _OWN_NAMESPACE)
            stage_process(proc, 8, ppid=7, rss_pages=1024, comm="python")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        row = next(line for line in context.splitlines() if line.startswith("pid=8 "))
        assert row.endswith(" detached")

    def test_unreadable_worker_file_keeps_the_ticket_and_names_the_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 8, ppid=1, rss_pages=1024, comm="python")
            worker_file = Path(directory) / "worker.pid"
            original_read_text = Path.read_text

            def unreadable_worker(path: Path, *args: object, **kwargs: object) -> str:
                if path == worker_file:
                    raise PermissionError
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", unreadable_worker):
                result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Worker attribution: unreadable (PermissionError)" in context

    def test_garbled_worker_file_keeps_the_ticket_and_names_the_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 8, ppid=1, rss_pages=1024, comm="python")
            (Path(directory) / "worker.pid").write_bytes(b"7\n\xff\n")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Worker attribution: unreadable (UnicodeDecodeError)" in context

    def test_invalid_holder_record_names_the_parse_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 8, ppid=1, rss_pages=1024, comm="python")
            (Path(directory) / "worker.pid").write_text("7\n{\n")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Worker attribution: unreadable (JSONDecodeError)" in context

    def test_swap_gets_snapshot_but_load_keeps_three_lines(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            swap = run_action_ladder(report("swap"))
            load = run_action_ladder(report("load"))
            assert swap is not None
            assert load is not None
            assert "Box memory snapshot" in Ticket.objects.get(pk=swap.firing.ticket_id).context
            assert len(Ticket.objects.get(pk=load.firing.ticket_id).context.splitlines()) == 3

    def test_second_firing_keeps_first_snapshot(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 20, ppid=1, rss_pages=512, comm="first")
            first = run_action_ladder(report("memory"))
            assert first is not None
            initial = Ticket.objects.get(pk=first.firing.ticket_id).context
            stage_process(proc, 21, ppid=1, rss_pages=1024, comm="second")
            second = run_action_ladder(report("memory", state_hash="second"))
            assert second is not None
            assert Ticket.objects.get(pk=second.firing.ticket_id).context == initial
            assert Ticket.objects.count() == 1

    def test_ticket_finished_after_pre_read_still_gets_snapshot(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            first = run_action_ladder(report("memory"))
            assert first is not None
            original_latest = actions.latest_firing

            def finish_after_pre_read(detector: str, dedup_key: str) -> SelfImproveFiring | None:
                firing = original_latest(detector, dedup_key)
                assert firing is not None
                ticket = firing.ticket
                assert ticket is not None
                Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.IGNORED)
                return firing

            with patch.object(actions, "latest_firing", side_effect=finish_after_pre_read):
                second = run_action_ladder(report("memory", state_hash="second"))
            assert second is not None
            assert second.firing.ticket_id != first.firing.ticket_id
            assert "Box memory snapshot" in Ticket.objects.get(pk=second.firing.ticket_id).context

    def test_unreadable_stat_and_missing_process_table_are_reported(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            stat = Path(directory) / "cgroup" / "memory.stat"
            stat.unlink()
            stat.mkdir()
            first = run_action_ladder(report("memory"))
            assert first is not None
            context = Ticket.objects.get(pk=first.firing.ticket_id).context
            assert "memory.stat: unreadable (IsADirectoryError)" in context
            with patch.object(memory_snapshot, "host_proc_root", return_value=(None, "unavailable")):
                second = run_action_ladder(report("swap"))
            assert second is not None
            assert "scan unavailable" in Ticket.objects.get(pk=second.firing.ticket_id).context

    def test_memory_stat_without_a_key_marks_it_unavailable_not_an_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            (Path(directory) / "cgroup" / "memory.stat").write_text(f"anon {7 * 1024 * _MIB}\n")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "memory.stat: anon=7168 MiB, shmem=unavailable" in context
        assert "KeyError" not in context

    def test_unreadable_cgroup_cap_and_usage_name_the_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            for name in ("memory.max", "memory.current"):
                file = Path(directory) / "cgroup" / name
                file.unlink()
                file.mkdir()
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
            assert "Cgroup cap: unreadable (IsADirectoryError)" in context
            assert "headroom: unreadable (IsADirectoryError)" in context

    def test_unreadable_cgroup_usage_alone_keeps_the_cap(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            usage = Path(directory) / "cgroup" / "memory.current"
            usage.unlink()
            usage.mkdir()
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Cgroup cap: 16384 MiB; headroom: unreadable (IsADirectoryError)" in context

    def test_cgroup_v1_files_supply_cap_and_headroom_when_v2_is_absent(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            cgroup = Path(directory) / "cgroup"
            (cgroup / "memory.max").unlink()
            (cgroup / "memory.current").unlink()
            (cgroup / "memory.limit_in_bytes").write_text(str(16 * 1024 * _MIB))
            (cgroup / "memory.usage_in_bytes").write_text(str(13 * 1024 * _MIB))
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Cgroup cap: 16384 MiB; headroom: 3072 MiB; box-scoped: yes" in context

    def test_unreadable_cgroup_v1_usage_is_unavailable_not_a_stale_v2_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            cgroup = Path(directory) / "cgroup"
            (cgroup / "memory.max").unlink()
            (cgroup / "memory.current").unlink()
            (cgroup / "memory.limit_in_bytes").write_text(str(16 * 1024 * _MIB))
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Cgroup cap: 16384 MiB; headroom: unavailable" in context

    def test_uncapped_cgroup_names_no_cap_for_headroom(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            (Path(directory) / "cgroup" / "memory.max").write_text("max\n")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
            assert "Judged RAM: 12288 MiB (host bound)" in context
            assert "Cgroup cap: no cap; headroom: no cap; box-scoped: no" in context

    def test_unreadable_meminfo_names_the_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            meminfo = Path(directory) / "meminfo"
            meminfo.unlink()
            meminfo.mkdir()
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
            assert "Host MemAvailable: unreadable (IsADirectoryError)" in context

    def test_readable_zero_memavailable_is_not_an_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            (Path(directory) / "meminfo").write_text("MemAvailable: 0 kB\n")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Host MemAvailable: 0 MiB" in context
        assert "unreadable (ValueError)" not in context

    def test_meminfo_without_memavailable_is_unavailable_not_an_error(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            (Path(directory) / "meminfo").write_text("MemTotal: 16777216 kB\n")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Host MemAvailable: unavailable" in context
        assert "KeyError" not in context

    def test_oversized_worker_pid_keeps_the_process_table_and_names_the_error(self) -> None:
        record = json.dumps({"pid_namespace": _OWN_NAMESPACE, "hostname": "box", "role": "worker"})
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 8, ppid=1, rss_pages=1024, comm="python")
            (Path(directory) / "worker.pid").write_text(f"99999999999999999999\n{record}\n")
            with patch("teatree.utils.singleton.pid_alive", actual_pid_alive):
                result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "Worker attribution: unreadable (OverflowError)" in context
        assert process_rows(context)["pid=8"].endswith(" detached")
        assert "Judged RAM: 4096 MiB (cgroup bound)" in context

    def test_absent_worker_file_is_reported_and_attributes_nothing(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            (Path(directory) / "worker.pid").unlink()
            stage_worker_tree(proc)
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert process_rows(context)["pid=11"].endswith(" detached")
        assert "Worker attribution: unreadable (FileNotFoundError)" in context

    def test_legacy_worker_file_without_a_namespace_attributes_nothing(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            (Path(directory) / "worker.pid").write_text("7\n")
            stage_worker_tree(proc)
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert process_rows(context)["pid=11"].endswith(" detached")
        assert "Worker attribution" not in context

    def test_dead_worker_attributes_nothing(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_worker_tree(proc)
            with patch("teatree.utils.singleton.pid_alive", return_value=False):
                result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert process_rows(context)["pid=11"].endswith(" detached")
        assert "Worker attribution" not in context

    def test_unreadable_process_table_is_unavailable(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            original_iterdir = Path.iterdir

            def guarded_iterdir(path: Path) -> Generator[Path]:
                if path == proc:
                    raise PermissionError
                yield from original_iterdir(path)

            with patch.object(Path, "iterdir", guarded_iterdir):
                result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
            assert "scan unavailable (no process table)" in context

    def test_bad_statm_is_skipped_and_root_cwd_is_not_a_ticket(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 20, ppid=1, rss_pages=512, comm="python")
            (proc / "20" / "statm").unlink()
            stage_process(proc, 21, ppid=1, rss_pages=256, comm="python")
            (proc / "21" / "cwd").symlink_to(Path(directory) / "worktrees")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
            assert "pid=20 " not in context
            assert "pid=21 " in context
            assert "ticket-dir=- detached" in context

    def test_top_processes_are_capped_at_ten(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            for pid in range(20, 32):
                stage_process(proc, pid, ppid=1, rss_pages=pid * 256, comm="python")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
            rows = [line for line in context.splitlines() if line.startswith("pid=")]
            assert len(rows) == 10
            assert rows[0].startswith("pid=31 ")
            assert rows[-1].startswith("pid=22 ")

    def test_working_directory_outside_every_overlay_worktree_root_is_not_a_ticket_dir(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 20, ppid=1, rss_pages=512, comm="python")
            scratch = Path(directory) / "scratch"
            (proc / "20" / "cwd").symlink_to(scratch / "review-checkout" / "repo")
            with patch.dict(os.environ, {DISPOSABLE_ROOTS_ENV: str(scratch)}):
                result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
        assert "pid=20 " in context
        assert "ticket-dir=- detached" in context

    def test_never_records_cmdline_environment_or_absolute_path(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)) as proc:
            stage_process(proc, 20, ppid=1, rss_pages=512, comm="python")
            (proc / "20" / "cwd").symlink_to(Path(directory) / "elsewhere")
            (proc / "20" / "cmdline").write_text("SECRET_MARKER /private/path")
            (proc / "20" / "environ").write_text("SECRET_MARKER=/private/path")
            result = run_action_ladder(report("memory"))
            assert result is not None
            context = Ticket.objects.get(pk=result.firing.ticket_id).context
            assert "SECRET_MARKER" not in context
            assert str(Path(directory)) not in context


class MemorySnapshotTransactionTests(TransactionTestCase):
    def test_process_scan_runs_before_atomic_ticket_write(self) -> None:
        with TemporaryDirectory() as directory, staged_box(Path(directory)):
            observed: list[bool] = []

            def table_root() -> tuple[Path, str]:
                observed.append(connection.in_atomic_block)
                return Path(directory) / "proc", ""

            with patch.object(memory_snapshot, "host_proc_root", side_effect=table_root):
                result = run_action_ladder(report("memory"))
            assert result is not None
            assert observed == [False]
