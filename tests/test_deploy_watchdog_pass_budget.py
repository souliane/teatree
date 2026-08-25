# test-path: cross-cutting — drives deploy/watchdog.sh (no src mirror).
"""Re-delivery is secondary; it must never starve the doctor pass (#4458).

``_drain_undelivered_pages`` was unbounded and ran AHEAD of ``run_doctor`` under
``timeout $PASS_TIMEOUT``, so a broken transport plus a handful of parked pages killed the
pass before the doctor ever ran. The supervisor went blind exactly when the box needed it,
and silently — a killed pass completes nothing and so reports nothing. The drain is now
bounded (per-pass row cap, wall-clock budget, per-exec ceiling), the doctor runs FIRST, and
the drain runs on every path including the compose-up failure that used to return ahead of it.

Runs the REAL ``run_pass`` against a stub ``docker`` whose ``notify send`` exec can be made
arbitrarily slow, and which records every doctor probe so "did the doctor run" is answerable.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parents[1] / "deploy" / "watchdog.sh"
_BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None or shutil.which("python3") is None,
    reason="needs bash + jq + python3 (present in the deploy image and CI)",
)

_GREEN = '{"ok": true, "findings": []}'

#: Stands in for the box's `timeout $PASS_TIMEOUT`: past it the pass is killed mid-flight.
_PASS_GUARD_SECONDS = 25


def _write_docker_stub(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" != compose ]; then exit 0; fi\n'
        "shift\n"
        'while [ "${1:-}" = -p ] || [ "${1:-}" = -f ]; do shift 2; done\n'
        'sub="${1:-}"; shift || true\n'
        'case "$sub" in\n'
        '  ps) printf "%s\\n" \'{"State":"exited","ExitCode":0}\' ;;\n'
        '  up) exit "${STUB_UP_RC:-0}" ;;\n'
        "  exec)\n"
        '    [ "${1:-}" = -T ] && shift\n'
        '    while [ "${1:-}" = -e ]; do shift 2; done\n'
        "    shift || true\n"
        '    case "$*" in\n'
        "      true) exit 0 ;;\n"
        '      *"doctor check --json"*)\n'
        '        printf "run\\n" >>"$STUB_DOCTOR_FILE"\n'
        '        printf "%s\\n" "$STUB_DOCTOR_JSON"; exit 1 ;;\n'
        '      *"notify send"*)\n'
        '        printf "attempt\\n" >>"$STUB_NOTIFY_ATTEMPTS"\n'
        '        [ "${STUB_NOTIFY_DELAY:-0}" = 0 ] || sleep "$STUB_NOTIFY_DELAY"\n'
        '        if [ "${STUB_NOTIFY_RC:-0}" != 0 ]; then exit "$STUB_NOTIFY_RC"; fi\n'
        '        cat >>"$STUB_NOTIFY_FILE"; printf "\\n" >>"$STUB_NOTIFY_FILE" ;;\n'
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class _Pass:
    """One ``run_pass`` outcome: what was DMed, what the log said, and whether it was killed."""

    def __init__(self, tmp_path: Path, log: str, *, timed_out: bool) -> None:
        self._tmp_path = tmp_path
        self.log = log
        self.timed_out = timed_out

    @property
    def doctor_runs(self) -> int:
        return len(self._read("doctor.txt").splitlines())

    @property
    def notify_attempts(self) -> int:
        return len(self._read("attempts.txt").splitlines())

    @property
    def dms(self) -> str:
        return self._read("dm.txt")

    @property
    def ledger(self) -> str:
        return self._read("undelivered.state")

    def _read(self, name: str) -> str:
        path = self._tmp_path / name
        return path.read_text(encoding="utf-8") if path.exists() else ""


def _run_pass(tmp_path: Path, *, guard: int = _PASS_GUARD_SECONDS, **stub_env: str) -> _Pass:
    bin_dir = tmp_path / "bin"
    _write_docker_stub(bin_dir)
    harness = tmp_path / "harness.sh"
    harness.write_text(f'set -uo pipefail\nsource "{WATCHDOG}"\nrun_pass\n', encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_NOTIFY_FILE"] = str(tmp_path / "dm.txt")
    env["STUB_DOCTOR_FILE"] = str(tmp_path / "doctor.txt")
    env["STUB_NOTIFY_ATTEMPTS"] = str(tmp_path / "attempts.txt")
    env["TEATREE_WATCHDOG_DOCTOR_RETRY_DELAY"] = "0"
    env["TEATREE_WATCHDOG_UNDELIVERED_STATE"] = str(tmp_path / "undelivered.state")
    env["TEATREE_WATCHDOG_RED_STATE"] = str(tmp_path / "red.state")
    env["TEATREE_WATCHDOG_DEPLOY_PENDING_STATE"] = str(tmp_path / "pending.state")
    env["TEATREE_WATCHDOG_LIVENESS_STATE"] = str(tmp_path / "liveness.state")
    env["TEATREE_WATCHDOG_DEPLOY_LOCK"] = str(tmp_path / "absent.lock")
    env.setdefault("STUB_DOCTOR_JSON", _GREEN)
    env.update(stub_env)

    timed_out = False
    log = ""
    try:
        done = subprocess.run(
            [_BASH, str(harness)], capture_output=True, text=True, check=False, env=env, timeout=guard
        )
        log = done.stderr
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        log = (expired.stderr or b"").decode("utf-8", "replace")
    return _Pass(tmp_path, log, timed_out=timed_out)


def _seed_ledger(tmp_path: Path, rows: int) -> None:
    """``rows`` distinct parked pages, oldest first — the shape a real backlog has."""
    (tmp_path / "undelivered.state").write_text(
        "".join(f"{1000 + n} parked-key-{n} Ym9keQ==\n" for n in range(rows)), encoding="utf-8"
    )


class TestTheDrainCannotStarveTheDoctor:
    def test_a_deep_backlog_on_a_slow_transport_still_runs_the_doctor(self, tmp_path: Path) -> None:
        # 20 rows x 2 exec services x 1s is 40s of drain against a 25s pass — unbounded,
        # that is the whole pass and the doctor never runs.
        _seed_ledger(tmp_path, 20)

        result = _run_pass(tmp_path, STUB_NOTIFY_RC="1", STUB_NOTIFY_DELAY="1")

        assert not result.timed_out, "the pass must complete inside its timeout, not be killed mid-drain"
        assert result.doctor_runs >= 1, "outage detection is the watchdog's primary job — it must have run"

    def test_the_drain_attempts_at_most_the_per_pass_cap(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path, 20)

        result = _run_pass(
            tmp_path,
            STUB_NOTIFY_RC="1",
            STUB_NOTIFY_DELAY="1",
            TEATREE_WATCHDOG_UNDELIVERED_DRAIN_MAX="3",
        )

        assert result.notify_attempts <= 3 * 2, "3 rows across 2 exec services is the ceiling"

    def test_rows_the_cap_deferred_are_kept_for_the_next_pass(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path, 20)

        result = _run_pass(
            tmp_path,
            STUB_NOTIFY_RC="1",
            STUB_NOTIFY_DELAY="1",
            TEATREE_WATCHDOG_UNDELIVERED_DRAIN_MAX="3",
        )

        assert len(result.ledger.splitlines()) == 20, "a deferred page is kept, never dropped"
        assert "deferred" in result.log, "a bounded drain must say what it did not attempt"

    def test_the_wall_clock_budget_stops_the_drain(self, tmp_path: Path) -> None:
        _seed_ledger(tmp_path, 20)

        result = _run_pass(
            tmp_path,
            STUB_NOTIFY_RC="1",
            STUB_NOTIFY_DELAY="1",
            TEATREE_WATCHDOG_UNDELIVERED_DRAIN_MAX="20",
            TEATREE_WATCHDOG_UNDELIVERED_DRAIN_BUDGET="3",
        )

        assert not result.timed_out
        assert result.notify_attempts < 20 * 2, "the budget must cut the drain short of the whole ledger"
        assert result.doctor_runs >= 1


class TestTheDrainRunsOnEveryPath:
    def test_a_failed_compose_up_still_drains_the_ledger(self, tmp_path: Path) -> None:
        # The strongest outage signal used to `return 0` ahead of the drain, so the README's
        # "every pass re-sends it" was false on exactly the path that matters most.
        _seed_ledger(tmp_path, 1)

        result = _run_pass(tmp_path, STUB_UP_RC="1")

        assert "the stack is DOWN" in result.dms, "the compose-up failure still pages"
        assert result.notify_attempts >= 2, "the parked page must be re-sent, not skipped"
        assert result.ledger == "", "a delivered page leaves the ledger on this path too"

    def test_a_fast_transport_drains_the_whole_ledger_in_one_pass(self, tmp_path: Path) -> None:
        # The anti-vacuous control: the bound must not stop a healthy drain from finishing.
        _seed_ledger(tmp_path, 3)

        result = _run_pass(tmp_path)

        assert result.ledger == ""
        assert result.doctor_runs >= 1
        assert "deferred" not in result.log


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
