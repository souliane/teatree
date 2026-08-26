"""``t3 tool reap-orphan-groups`` — the reclaim the doctor's WARN names (#4580).

The controls here are REAL leaderless groups, planted per test: a session leader that
exits while a CPU-burning child survives is exactly the incident's shape (members
reparented to PID 1, group leader gone, still runnable), and this box carries none of its
own — so a command that reaped nothing would pass a "clean box" assertion.

One class below cannot plant its subject. A pid numbered in the HOST's namespace cannot be
created from inside this container at all, which is the very reason the refusal exists, so
that group is constructed directly and the reader is stubbed. Everything else is real.
"""

import contextlib
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import django.test
from typer.testing import CliRunner

from teatree import request_cache
from teatree.cli import app
from teatree.cli.reap_orphan_groups import refusal_for
from teatree.core.cleanup.orphan_process_groups import GroupMember, OrphanGroup, OrphanSurvey
from teatree.core.models import ConfigSetting

runner = CliRunner()

_SURVEY = "teatree.cli.reap_orphan_groups.survey_orphan_groups"
_SETTLE_SECONDS = 0.4
_POLL_SECONDS = 0.05
_WAIT_SECONDS = 5.0


def _group_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


#: A bounded CPU burner: runnable (so the group is detected) and self-terminating, so a
#: test that dies before its cleanup cannot leave the box burning a core.
_BURN_PY = "import time" + chr(10) + "e = time.time() + 60" + chr(10) + "while time.time() < e: pass"
#: Iteration-bounded rather than clock-bounded: /bin/sh here is dash, which has no $SECONDS.
_BURN_SH = "i=0; while [ $i -lt 90000000 ]; do i=$((i+1)); done"


@contextmanager
def planted_leaderless_group(*, program: str = "sh", tail: str = "") -> Iterator[int]:
    """A real group whose leader has exited and whose child is still burning CPU.

    *program* selects the surviving child's PROGRAM WORD, which is what the never-reap
    rules key on — ``sh`` is unprotected, a ``python`` running teatree code is not.
    """
    burner = _BURN_SH if program == "sh" else _BURN_PY
    argv0 = "sh" if program == "sh" else sys.executable
    child = f"{argv0} -c {shlex.quote(burner)}"
    leader = subprocess.Popen(  # noqa: S602 — the shell IS the subject: it plants the group
        f"{child} {tail} & exec sleep 0",
        shell=True,
        start_new_session=True,
    )
    pgid = os.getpgid(leader.pid)
    leader.wait()
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline and Path("/proc", str(leader.pid)).exists():
        time.sleep(_POLL_SECONDS)
    try:
        yield pgid
    finally:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGKILL)


def _reap_now(*args: str):
    ConfigSetting.objects.set_value("orphan_group_min_age_hours", 0)
    request_cache.invalidate()
    return runner.invoke(app, ["tool", "reap-orphan-groups", *args])


def _synthetic(
    *,
    signalable: bool = True,
    argv: tuple[str, ...] = ("/bin/bash", "-c", "while :"),
    pgid: int = 4076652,
) -> OrphanGroup:
    return OrphanGroup(
        pgid=pgid,
        members=(GroupMember(pid=pgid + 1, comm="bash", state="R", argv=argv),),
        age_seconds=9.4 * 24 * 3600,
        cpu_seconds=5.4 * 24 * 3600,
        signalable=signalable,
        source="/proc" if signalable else "/host-proc",
    )


class TestReapsARealLeaderlessGroup(django.test.TestCase):
    def test_dry_run_names_the_group_and_leaves_it_running(self) -> None:
        with planted_leaderless_group() as pgid:
            result = _reap_now("--pgid", str(pgid))

            assert result.exit_code == 0, result.output
            assert str(pgid) in result.output
            assert "--apply" in result.output
            time.sleep(_SETTLE_SECONDS)
            assert _group_is_alive(pgid) is True

    def test_apply_terms_the_group(self) -> None:
        with planted_leaderless_group() as pgid:
            result = _reap_now("--pgid", str(pgid), "--apply")

            assert result.exit_code == 0, result.output
            assert _group_is_alive(pgid) is False

    def test_a_second_apply_on_a_reaped_group_is_a_no_op(self) -> None:
        with planted_leaderless_group() as pgid:
            assert _reap_now("--pgid", str(pgid), "--apply").exit_code == 0
            # Idempotence: the group is already gone, so it is simply not found again.
            assert _reap_now("--pgid", str(pgid), "--apply").exit_code == 0

    def test_an_unrequested_group_is_untouched_when_one_pgid_is_named(self) -> None:
        with planted_leaderless_group() as kept, planted_leaderless_group() as reaped:
            assert _reap_now("--pgid", str(reaped), "--apply").exit_code == 0

            assert _group_is_alive(reaped) is False
            time.sleep(_SETTLE_SECONDS)
            assert _group_is_alive(kept) is True


class TestRefusesWhatItMustNotSignal(django.test.TestCase):
    def test_a_teatree_path_in_the_arguments_does_not_protect_an_unrelated_program(self) -> None:
        # The defect dogfooding caught: every checkout on this box sits under a path
        # containing "teatree", so matching the whole command line protected every
        # process on the machine and the reaper was a silent no-op.
        under_a_teatree_path = ("/opt/build/teatree/tools/sh", "-c", "while :")

        assert refusal_for(_synthetic(argv=under_a_teatree_path), protected_pgids=set()) == ""

    def test_a_host_namespace_group_is_refused_and_never_signalled(self) -> None:
        # A host pid cannot be created from this container; the refusal is the reason.
        group = _synthetic(signalable=False)
        with (
            patch(_SURVEY, return_value=OrphanSurvey(groups=(group,), gaps=())),
            patch("os.killpg") as killpg,
        ):
            result = _reap_now("--pgid", str(group.pgid), "--apply")

        assert result.exit_code == 1
        assert killpg.call_count == 0
        assert "kill -TERM -4076652" in result.output

    def test_a_protected_member_refuses_a_real_group(self) -> None:
        # A python process running teatree code — the shape the never-reap rule exists for.
        with planted_leaderless_group(program="python", tail="teatree") as pgid, patch("os.killpg") as killpg:
            result = _reap_now("--pgid", str(pgid), "--apply")

        assert result.exit_code == 1
        assert killpg.call_count == 0
        assert "teatree" in result.output

    def test_this_process_group_is_refused(self) -> None:
        own = _synthetic(pgid=os.getpgid(0))

        assert refusal_for(own, protected_pgids={os.getpgid(0)}) != ""

    def test_the_init_group_is_refused(self) -> None:
        assert refusal_for(_synthetic(pgid=1), protected_pgids=set()) != ""

    def test_an_ordinary_group_is_permitted(self) -> None:
        # The control for the four refusals above: without it they could all be
        # satisfied by a predicate that refuses everything.
        assert refusal_for(_synthetic(), protected_pgids=set()) == ""


class TestReportsWhatItCannotSee(django.test.TestCase):
    def test_a_gap_is_printed_rather_than_read_as_nothing_to_do(self) -> None:
        with patch(_SURVEY, return_value=OrphanSurvey(groups=(), gaps=("no host process table",))):
            result = _reap_now()

        assert "no host process table" in result.output


class TestRegisteredOnTheToolApp(django.test.TestCase):
    def test_the_command_is_reachable(self) -> None:
        assert runner.invoke(app, ["tool", "reap-orphan-groups", "--help"]).exit_code == 0
