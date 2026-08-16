"""``probe_deploy_liveness`` — a convergence in flight, provably gone, or unknowable here (#4359).

The clear that resumes admission is authorised by DEADNESS, never by age alone, so this
probe's whole job is to never answer GONE on evidence it does not have: the deploy's own
in-progress record and a host-covering process table must BOTH answer, or the verdict is
UNKNOWN and the detector reports instead of repairing.
"""

import time
from pathlib import Path

import pytest

from teatree.cli.doctor import deploy_liveness
from teatree.cli.doctor.deploy_liveness import DeployLiveness, DeployView, probe_deploy_liveness, resolve_deploy_view

#: Stands in for the caller's own convergence budget (``quiescing_deploy_budget_seconds``).
_RECORD_MAX_AGE = 4800.0

_DEPLOY_CMDLINE = "/bin/bash\x00/srv/checkout/deploy/deploy.sh\x00"
_OTHER_CMDLINE = "/usr/bin/python3\x00-m\x00teatree.worker\x00"
_DIRECT_DEPLOY_CMDLINE = "/srv/checkout/deploy/deploy.sh\x00"
#: NAMES the script as plain argv data — never invokes it. The bug this guards: a
#: bare substring test over the whole cmdline reads this as a live convergence too.
_DECOY_CMDLINE = "/usr/bin/tail\x00-f\x00/dev/null\x00deploy/deploy.sh\x00"


def _lock(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "teatree-deploy.lock"
    path.write_text(body, encoding="utf-8")
    return path


def _proc_root(tmp_path: Path, *cmdlines: str) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    for pid, cmdline in enumerate(cmdlines, start=101):
        entry = root / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(cmdline.encode("utf-8"))
    return root


def _record(*, age_seconds: float) -> str:
    return f"4242 {int(time.time() - age_seconds)}\n"


class TestALiveConvergenceIsNeverCalledGone:
    def test_a_fresh_in_progress_record_is_a_live_convergence(self, tmp_path: Path) -> None:
        view = DeployView(
            lock=_lock(tmp_path, _record(age_seconds=30)),
            proc_root=_proc_root(tmp_path, _OTHER_CMDLINE),
        )

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.LIVE

    def test_a_running_deploy_outlives_its_own_stamp(self, tmp_path: Path) -> None:
        # The record is stamped ONCE at the convergence's start, so a drain longer than
        # the ceiling ages it out while deploy.sh is still very much alive. The process
        # table is what keeps that stale stamp from reading as a dead deploy.
        view = DeployView(
            lock=_lock(tmp_path, _record(age_seconds=_RECORD_MAX_AGE * 2)),
            proc_root=_proc_root(tmp_path, _OTHER_CMDLINE, _DEPLOY_CMDLINE),
        )

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.LIVE


class TestOnlyAnActualInvocationReadsAsLive:
    def test_a_process_merely_naming_the_script_is_never_live(self, tmp_path: Path) -> None:
        # `tail -f /dev/null deploy/deploy.sh` — equally `cat`/`grep`/an editor opening
        # it — is a command that NAMES the path as an argument, not a running
        # convergence. Only `argv[0]`/the argument right after a shell interpreter
        # naming the script as THE THING BEING RUN may answer LIVE.
        view = DeployView(
            lock=_lock(tmp_path, ""),
            proc_root=_proc_root(tmp_path, _OTHER_CMDLINE, _DECOY_CMDLINE),
        )

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.GONE

    def test_a_directly_executed_script_is_live(self, tmp_path: Path) -> None:
        # No interpreter argv[0] — the script exec'd via its own shebang, argv[0] IS
        # the script path.
        view = DeployView(lock=_lock(tmp_path, ""), proc_root=_proc_root(tmp_path, _DIRECT_DEPLOY_CMDLINE))

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.LIVE


class TestDeadnessIsOnlyReportedWhenBothSignalsAnswer:
    def test_a_retired_record_and_a_table_with_no_deploy_is_gone(self, tmp_path: Path) -> None:
        # deploy.sh clears the record on exit, so an emptied lock file is the ordinary
        # fingerprint of a convergence that has finished or died.
        view = DeployView(lock=_lock(tmp_path, ""), proc_root=_proc_root(tmp_path, _OTHER_CMDLINE))

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.GONE

    def test_a_stamp_older_than_the_ceiling_with_no_deploy_process_is_gone(self, tmp_path: Path) -> None:
        view = DeployView(
            lock=_lock(tmp_path, _record(age_seconds=_RECORD_MAX_AGE + 60)),
            proc_root=_proc_root(tmp_path, _OTHER_CMDLINE),
        )

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.GONE


class TestWhatItCannotEstablishItRefuses:
    def test_an_unreachable_lock_is_unknown(self, tmp_path: Path) -> None:
        view = DeployView(lock=None, proc_root=_proc_root(tmp_path, _OTHER_CMDLINE))

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.UNKNOWN

    def test_a_record_shape_this_venue_cannot_date_is_unknown(self, tmp_path: Path) -> None:
        view = DeployView(
            lock=_lock(tmp_path, "converging\n"),
            proc_root=_proc_root(tmp_path, _OTHER_CMDLINE),
        )

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.UNKNOWN

    def test_no_host_covering_process_table_is_unknown(self, tmp_path: Path) -> None:
        # The containerised doctor whose deployment mounts no /host-proc: its own /proc
        # lists this container's namespace, where a host deploy.sh reads as absent.
        view = DeployView(lock=_lock(tmp_path, ""), proc_root=None)

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.UNKNOWN

    def test_a_process_table_listing_nothing_is_unknown(self, tmp_path: Path) -> None:
        view = DeployView(lock=_lock(tmp_path, ""), proc_root=_proc_root(tmp_path))

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.UNKNOWN

    def test_an_unreadable_process_table_is_unknown(self, tmp_path: Path) -> None:
        view = DeployView(lock=_lock(tmp_path, ""), proc_root=tmp_path / "absent-proc")

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.UNKNOWN

    def test_a_pid_that_will_not_say_what_it_runs_does_not_decide_the_verdict(self, tmp_path: Path) -> None:
        # A per-process read can be refused (another uid's process, or one exiting under
        # the scan); the remaining pids still answer, so the table as a whole does.
        root = _proc_root(tmp_path, _OTHER_CMDLINE)
        (root / "999").mkdir()
        view = DeployView(lock=_lock(tmp_path, ""), proc_root=root)

        assert probe_deploy_liveness(record_max_age=_RECORD_MAX_AGE, view=view) is DeployLiveness.GONE


class TestTheVenueResolution:
    def test_an_absent_lock_file_resolves_to_no_lock_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEATREE_DEPLOY_LOCK", str(tmp_path / "never-deployed.lock"))

        assert resolve_deploy_view().lock is None

    def test_the_deploys_own_lock_variable_names_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lock = _lock(tmp_path, "")
        monkeypatch.setenv("TEATREE_DEPLOY_LOCK", str(lock))

        assert resolve_deploy_view().lock == lock

    def test_a_containerised_venue_reads_the_host_temp_mount(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # deploy.sh writes the lock on the HOST; the container's own /tmp holds nothing,
        # so reading it there would report every convergence as never having run.
        host_tmp = tmp_path / "host-tmp"
        host_tmp.mkdir()
        lock = _lock(host_tmp, "")
        monkeypatch.delenv("TEATREE_DEPLOY_LOCK", raising=False)
        monkeypatch.setattr(deploy_liveness, "_HOST_TMP", host_tmp)

        assert resolve_deploy_view().lock == lock

    def test_a_venue_with_no_host_mount_reads_its_own_temp_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        venue_tmp = tmp_path / "tmp"
        venue_tmp.mkdir()
        lock = _lock(venue_tmp, "")
        monkeypatch.delenv("TEATREE_DEPLOY_LOCK", raising=False)
        monkeypatch.setattr(deploy_liveness, "_HOST_TMP", tmp_path / "absent-host-tmp")
        monkeypatch.setattr(deploy_liveness, "_VENUE_TMP", venue_tmp)

        assert resolve_deploy_view().lock == lock
