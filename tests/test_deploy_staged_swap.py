# test-path: cross-cutting — drives deploy/deploy.sh (no src mirror).
"""A convergence must never leave the control plane wholly absent (#4214).

One all-at-once recreate replaces admin, worker and slack-listener together, and
each waits on ``teatree-init: service_completed_successfully`` — so the dashboard
and the only control-DB CLI route sit in ``Created`` for the whole init window
(67s measured on the box). Every ``t3`` call inside it fails the way a real
outage does, and the recreate destroys the container logs a live diagnosis was
reading.

Runs the REAL ``deploy/deploy.sh`` against a stub ``docker``/``curl``/``systemctl``
and asserts the argv sequence it actually issues, so the ordering is proved from
the shipped script rather than from a re-typed copy of it.
"""

import os
import shutil
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DEPLOY_DIR = _ROOT / "deploy"
_BASH = shutil.which("bash") or "bash"
_GIT = shutil.which("git") or "git"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("flock") is None,
    reason="needs bash + flock (present in the deploy image and CI)",
)

_SERVICES = "teatree-init teatree-worker teatree-admin teatree-slack-listener teatree-watchdog"

_DOCKER_STUB = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>"${{STUB_DOCKER_LOG:-/dev/null}}"
stub_worker_state() {{
    local state="${{STUB_WORKER_STATUS:-running}}"
    [ -f "${{STUB_WORKER_STATE_FILE:-/dev/null}}" ] && state="$(cat "$STUB_WORKER_STATE_FILE")"
    printf '%s\\n' "$state"
}}
if [ "$1" = inspect ]; then
    case "${{3:-}}" in
    *"State.ExitCode"*)
        printf '%s\\n' "${{STUB_INIT_STATE:-exited 0}}"
        ;;
    *"RestartCount"*)
        [ -z "${{STUB_WORKER_INSPECT_EXIT:-}}" ] || exit "$STUB_WORKER_INSPECT_EXIT"
        worker_state="$(stub_worker_state)"
        printf '%s/%s\\n' "$worker_state" "${{STUB_WORKER_RESTART_COUNT:-0}}"
        ;;
    *"State.Status"*)
        [ -z "${{STUB_WORKER_INSPECT_EXIT:-}}" ] || exit "$STUB_WORKER_INSPECT_EXIT"
        worker_state="$(stub_worker_state)"
        printf '%s\\n' "$worker_state"
        ;;
    esac
    exit 0
fi
if [ "$1" != compose ]; then
    exit 0
fi
shift
while [ "${{1:-}}" = -f ] || [ "${{1:-}}" = -p ]; do shift 2; done
sub="${{1:-}}"
shift || true
case "$sub" in
exec)
    while :; do
        case "${{1:-}}" in
        -T) shift ;;
        --env) shift 2 ;;
        *) break ;;
        esac
    done
    shift || true
    case "$*" in
    *"worker status"*)
        [ -z "${{STUB_WORKER_STATUS_EXIT:-}}" ] || exit "$STUB_WORKER_STATUS_EXIT"
        if [ "$(stub_worker_state)" = running ]; then
            printf '{{"running": true}}\\n'
        else
            printf '{{"running": false}}\\n'
            exit 1
        fi
        ;;
    *"worker drain"*)
        drain_count=0
        [ -f "${{STUB_DRAIN_COUNT_FILE:-/dev/null}}" ] && drain_count="$(cat "$STUB_DRAIN_COUNT_FILE")"
        drain_count=$((drain_count + 1))
        printf '%s' "$drain_count" >|"${{STUB_DRAIN_COUNT_FILE}}"
        if [ -n "${{STUB_DRAIN_FAIL_AFTER:-}}" ] && [ "$drain_count" -gt "$STUB_DRAIN_FAIL_AFTER" ]; then
            exit 1
        fi
        exit "${{STUB_DRAIN_EXIT:-${{STUB_EXEC_EXIT:-0}}}}"
        ;;
    esac
    exit "${{STUB_EXEC_EXIT:-0}}"
    ;;
ps)
    case "$*" in
    *"--all --quiet teatree-init"*) printf '%s\\n' "${{STUB_CONTAINER_ID:-stubcid}}" ;;
    *"--all --quiet teatree-worker"*)
        [ "$(stub_worker_state)" = absent ] || printf '%s\\n' "${{STUB_CONTAINER_ID:-stubcid}}"
        ;;
    *"-q teatree-worker"*)
        case "$(stub_worker_state)" in
        running|restarting) printf '%s\\n' "${{STUB_CONTAINER_ID:-stubcid}}" ;;
        esac
        ;;
    *--quiet*) printf '%s\\n' "${{STUB_CONTAINER_ID:-stubcid}}" ;;
    esac
    exit 0
    ;;
config)
    printf '%s\\n' {" ".join(_SERVICES.split())}
    exit 0
    ;;
build) exit "${{STUB_BUILD_EXIT:-0}}" ;;
stop)
    printf '%s' "${{STUB_STOP_STATE:-exited}}" >|"${{STUB_WORKER_STATE_FILE}}"
    if [ -n "${{STUB_ORPHAN_STOP:-}}" ]; then
        echo "Error response from daemon: No such container: deadbeef1234" >&2
        exit 1
    fi
    exit "${{STUB_STOP_EXIT:-0}}"
    ;;
up)
    case " $* " in
    *" teatree-worker "*) printf '%s' running >|"${{STUB_WORKER_STATE_FILE}}" ;;
    esac
    case " $* " in
    *" ${{STUB_ORPHAN_UP:-<>}} "*)
        echo "Error response from daemon: No such container: deadbeef1234" >&2
        exit 1
        ;;
    esac
    exit "${{STUB_UP_EXIT:-0}}"
    ;;
esac
exit 0
"""

# Answers until STUB_CURL_FAIL_AFTER calls have been served, so a test can model a
# dashboard that was up before its swap and never came back after it.
_CURL_STUB = """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >>"${STUB_DOCKER_LOG:-/dev/null}"
n=0
if [ -n "${STUB_CURL_COUNT:-}" ]; then
    [ -f "$STUB_CURL_COUNT" ] && n="$(cat "$STUB_CURL_COUNT")"
    n=$((n + 1))
    printf '%s' "$n" >|"$STUB_CURL_COUNT"
fi
if [ -n "${STUB_CURL_FAIL_AFTER:-}" ] && [ "$n" -gt "$STUB_CURL_FAIL_AFTER" ]; then
    exit 22
fi
exit 0
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A minimal repo checkout carrying the REAL deploy.sh and compose files."""
    root = tmp_path / "checkout"
    deploy = root / "deploy"
    deploy.mkdir(parents=True)
    for name in ("deploy.sh", "docker-compose.yml", "docker-compose.host-identity.yml"):
        shutil.copy2(_DEPLOY_DIR / name, deploy / name)
    (deploy / "deploy.sh").chmod(0o755)
    (deploy / "teatree.env").write_text("", encoding="utf-8")
    _write_exec(deploy / "fast-forward-checkout.sh", "#!/usr/bin/env bash\nexit 0\n")

    probe = root / "src" / "teatree" / "utils"
    probe.mkdir(parents=True)
    (probe / "ram_probe.py").write_text("print('TEATREE_WORKER_CPUS=1.0')\n", encoding="utf-8")

    subprocess.run([_GIT, "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run([_GIT, "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [_GIT, "-C", str(root), "-c", "user.email=fixture", "-c", "user.name=fixture", "commit", "-qm", "seed"],
        check=True,
    )
    return root


def _run(checkout: Path, tmp_path: Path, **env_extra: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    _write_exec(stub_bin / "docker", _DOCKER_STUB)
    _write_exec(stub_bin / "curl", _CURL_STUB)
    _write_exec(stub_bin / "systemctl", "#!/usr/bin/env bash\nexit 0\n")

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    docker_log = tmp_path / "docker.log"

    env = dict(os.environ)
    env.update(
        PATH=f"{stub_bin}{os.pathsep}{env['PATH']}",
        HOME=str(home),
        GITLAB_TOKEN="stub-token",
        NOTION_TOKEN="stub-token",
        STUB_DOCKER_LOG=str(docker_log),
        STUB_CURL_COUNT=str(tmp_path / "curl.count"),
        STUB_DRAIN_COUNT_FILE=str(tmp_path / "drain.count"),
        STUB_WORKER_STATE_FILE=str(tmp_path / "worker.state"),
        TEATREE_DEPLOY_LOCK=str(tmp_path / "deploy.lock"),
        TEATREE_DEPLOY_LOG_ARCHIVE_DIR=str(tmp_path / "archive"),
        TEATREE_ADMIN_SWAP_BUDGET="2",
        TEATREE_INIT_WAIT_TIMEOUT="2",
        TEATREE_RESUME_TIMEOUT="2",
        TEATREE_DRAIN_TIMEOUT="5",
    )
    env.update(env_extra)

    proc = subprocess.run(
        [_BASH, str(checkout / "deploy" / "deploy.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(checkout),
        check=False,
    )
    calls = docker_log.read_text(encoding="utf-8").splitlines() if docker_log.exists() else []
    return proc, calls


def _compose_args(call: str) -> list[str]:
    """The compose subcommand and its arguments, with the `-f <file>` pairs dropped."""
    tokens = call.split()
    if not tokens or tokens[0] != "compose":
        return []
    rest = tokens[1:]
    while rest[:1] in (["-f"], ["-p"]):
        rest = rest[2:]
    return rest


def _index_of(calls: Iterable[str], predicate) -> int:
    for i, call in enumerate(calls):
        if predicate(_compose_args(call)):
            return i
    return -1


def _is_up(args: list[str]) -> bool:
    return args[:1] == ["up"]


def _up_services(args: list[str]) -> list[str]:
    return [a for a in args[1:] if not a.startswith("-")]


def _assert_fresh_worker_route_precedes_admin_swap(calls: list[str], *, after: int) -> None:
    worker_at = _index_of(calls, lambda a: _is_up(a) and "teatree-worker" in _up_services(a))
    worker_ready_at = _index_of(calls, lambda a: "worker status --json" in " ".join(a))
    admin_at = _index_of(calls, lambda a: _is_up(a) and "teatree-admin" in _up_services(a))
    admin_ready_at = next((i for i, call in enumerate(calls) if i > admin_at and call.startswith("curl ")), -1)
    final_worker_ready_at = next(
        (
            i
            for i, call in enumerate(calls)
            if i > admin_ready_at and "worker status --json" in " ".join(_compose_args(call))
        ),
        -1,
    )
    assert worker_at != -1, "the contained worker is never recreated"
    assert worker_ready_at != -1, "the fresh worker route is never positively proved"
    assert admin_at != -1, "the admin is never swapped"
    assert after < worker_at < worker_ready_at < admin_at, (
        "a contained worker must be recreated and proved before the old admin is swapped"
    )
    assert admin_at < admin_ready_at < final_worker_ready_at, (
        "the successful deploy must finish with both the admin and worker routes proved"
    )

    admin_serving = True
    worker_serving = False
    for call in calls[after + 1 :]:
        args = _compose_args(call)
        if _is_up(args) and "teatree-worker" in _up_services(args):
            worker_serving = False
        elif "worker status --json" in " ".join(args):
            worker_serving = True
        if _is_up(args) and "teatree-admin" in _up_services(args):
            admin_serving = False
        elif call.startswith("curl "):
            admin_serving = True
        assert admin_serving or worker_serving, f"both control-plane routes are down at: {call}"
    assert admin_serving
    assert worker_serving


class TestTheControlPlaneIsNeverWhollyAbsent:
    def test_no_single_recreate_takes_the_dashboard_and_the_worker_together(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        _, calls = _run(checkout, tmp_path)
        ups = [_compose_args(c) for c in calls if _is_up(_compose_args(c))]
        assert ups, "the convergence issued no `up` at all"
        for args in ups:
            services = _up_services(args)
            assert services, f"a bare `up` recreates every service at once: {args!r}"
            together = {"teatree-admin", "teatree-worker"} <= set(services)
            assert not together, f"admin and worker are recreated by one call: {args!r}"

    def test_the_dashboard_is_swapped_before_the_worker(self, checkout: Path, tmp_path: Path) -> None:
        _, calls = _run(checkout, tmp_path)
        admin_at = _index_of(calls, lambda a: _is_up(a) and "teatree-admin" in _up_services(a))
        worker_at = _index_of(calls, lambda a: _is_up(a) and "teatree-worker" in _up_services(a))
        assert admin_at != -1, "teatree-admin is never recreated on its own"
        assert worker_at != -1, "teatree-worker is never recreated on its own"
        assert admin_at < worker_at, "the worker must stay the live route until the dashboard answers again"

    def test_the_image_is_built_before_anything_is_recreated(self, checkout: Path, tmp_path: Path) -> None:
        _, calls = _run(checkout, tmp_path)
        build_at = _index_of(calls, lambda a: a[:1] == ["build"])
        first_up = _index_of(calls, _is_up)
        assert build_at != -1, "the build must be its own step so it recreates nothing while it runs"
        assert first_up != -1
        assert build_at < first_up

    def test_init_is_run_alone_and_waited_for_before_the_dashboard_moves(self, checkout: Path, tmp_path: Path) -> None:
        _, calls = _run(checkout, tmp_path)
        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert init_at != -1, "init must be brought up on its own, with the old generation still serving"
        inspected_at = next((i for i, c in enumerate(calls) if "State.ExitCode" in c), -1)
        admin_at = _index_of(calls, lambda a: _is_up(a) and "teatree-admin" in _up_services(a))
        assert init_at < inspected_at < admin_at, "init's exit must be observed before the dashboard is swapped"

    def test_the_converge_step_never_replays_the_one_shot_init(self, checkout: Path, tmp_path: Path) -> None:
        _, calls = _run(checkout, tmp_path)
        init_ups = [a for c in calls if _is_up(a := _compose_args(c)) and "teatree-init" in _up_services(a)]
        assert len(init_ups) == 1, f"`up` on an exited one-shot replays the whole init: {init_ups!r}"

    def test_every_remaining_service_is_still_converged(self, checkout: Path, tmp_path: Path) -> None:
        _, calls = _run(checkout, tmp_path)
        recreated = {s for c in calls if _is_up(a := _compose_args(c)) for s in _up_services(a)}
        assert set(_SERVICES.split()) <= recreated, f"a declared service was never converged: {recreated!r}"


class TestInFlightWorkSurvivesTheSwap:
    def test_the_worker_is_drained_before_migrations_and_again_before_its_swap(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        _, calls = _run(checkout, tmp_path)
        drains = [i for i, c in enumerate(calls) if "worker drain" in c]
        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        worker_at = _index_of(calls, lambda a: _is_up(a) and "teatree-worker" in _up_services(a))
        assert len(drains) == 2, f"init clears worker_quiescing, so the gate must be re-asserted after it: {drains!r}"
        assert drains[0] < init_at, "in-flight agents must finish before migrations run"
        assert init_at < drains[1] < worker_at, "the second drain must sit between init and the worker swap"

    def test_admission_is_resumed_on_the_fresh_worker(self, checkout: Path, tmp_path: Path) -> None:
        _, calls = _run(checkout, tmp_path)
        resume_at = next((i for i, c in enumerate(calls) if "worker_quiescing false" in c), -1)
        worker_at = _index_of(calls, lambda a: _is_up(a) and "teatree-worker" in _up_services(a))
        assert resume_at != -1, "nothing re-opens admission once init's own clear has already run"
        assert worker_at < resume_at

    def test_a_crash_loop_is_stopped_before_init_when_it_cannot_be_drained(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        proc, calls = _run(
            checkout,
            tmp_path,
            STUB_WORKER_STATUS="restarting",
            STUB_WORKER_RESTART_COUNT="3",
            STUB_DRAIN_EXIT="1",
        )

        stop_at = _index_of(calls, lambda a: a[:2] == ["stop", "teatree-worker"])
        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode == 0, proc.stderr
        assert stop_at != -1, "a worker that cannot drain must be contained before migrations"
        assert stop_at < init_at
        _assert_fresh_worker_route_precedes_admin_swap(calls, after=init_at)

    def test_a_crash_loop_is_stopped_even_when_a_drain_exec_temporarily_answers(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        proc, calls = _run(
            checkout,
            tmp_path,
            STUB_WORKER_STATUS="restarting",
            STUB_WORKER_RESTART_COUNT="3",
        )

        stop_at = _index_of(calls, lambda a: a[:2] == ["stop", "teatree-worker"])
        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode == 0, proc.stderr
        assert stop_at != -1, "a restart loop is not contained by one successful exec"
        assert stop_at < init_at

    def test_a_failed_drain_does_not_stop_the_running_worker_when_admin_is_down(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        proc, calls = _run(checkout, tmp_path, STUB_DRAIN_EXIT="1", STUB_CURL_FAIL_AFTER="0")

        stop_at = _index_of(calls, lambda a: a[:2] == ["stop", "teatree-worker"])
        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode != 0
        assert stop_at == -1, "the worker is the sole control-plane route while admin is down"
        assert init_at == -1
        assert "admin" in proc.stderr

    def test_a_failed_drain_stops_the_running_worker_only_after_admin_answers(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        proc, calls = _run(checkout, tmp_path, STUB_DRAIN_EXIT="1")

        admin_probe_at = next((i for i, call in enumerate(calls) if call.startswith("curl ")), -1)
        stop_at = _index_of(calls, lambda a: a[:2] == ["stop", "teatree-worker"])
        stopped_state_at = _index_of(calls, lambda a: a[:4] == ["ps", "--all", "--quiet", "teatree-worker"])
        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode == 0, proc.stderr
        assert admin_probe_at < stop_at < stopped_state_at < init_at
        _assert_fresh_worker_route_precedes_admin_swap(calls, after=init_at)

    def test_second_failed_drain_restores_the_worker_route_before_admin_swap(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        proc, calls = _run(checkout, tmp_path, STUB_DRAIN_FAIL_AFTER="1")

        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        stop_at = _index_of(calls, lambda a: a[:2] == ["stop", "teatree-worker"])
        assert proc.returncode == 0, proc.stderr
        assert init_at != -1
        assert init_at < stop_at, "this must exercise containment after init"
        _assert_fresh_worker_route_precedes_admin_swap(calls, after=stop_at)

    def test_an_unproven_fresh_worker_route_leaves_the_old_admin_serving(self, checkout: Path, tmp_path: Path) -> None:
        proc, calls = _run(
            checkout,
            tmp_path,
            STUB_WORKER_STATUS="restarting",
            STUB_WORKER_STATUS_EXIT="1",
            TEATREE_RESUME_TIMEOUT="1",
        )

        worker_at = _index_of(calls, lambda a: _is_up(a) and "teatree-worker" in _up_services(a))
        worker_ready_at = _index_of(calls, lambda a: "worker status --json" in " ".join(a))
        admin_at = _index_of(calls, lambda a: _is_up(a) and "teatree-admin" in _up_services(a))
        assert proc.returncode != 0
        assert worker_at < worker_ready_at
        assert admin_at == -1, "an unproven worker cannot protect the admin swap"
        assert "fresh worker route did not answer" in proc.stderr

    def test_second_failed_drain_preserves_the_worker_when_admin_is_down_after_init(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        proc, calls = _run(
            checkout,
            tmp_path,
            STUB_DRAIN_FAIL_AFTER="1",
            STUB_CURL_FAIL_AFTER="0",
        )

        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        stop_at = _index_of(calls, lambda a: a[:2] == ["stop", "teatree-worker"])
        worker_at = _index_of(calls, lambda a: _is_up(a) and "teatree-worker" in _up_services(a))
        assert proc.returncode != 0
        assert init_at != -1, "the first drain must succeed so this proves the post-init path"
        assert stop_at == -1
        assert worker_at == -1

    @pytest.mark.parametrize("stopped_state", ["absent", "exited"])
    def test_a_nonzero_stop_aborts_even_when_the_worker_looks_stopped(
        self, checkout: Path, tmp_path: Path, stopped_state: str
    ) -> None:
        proc, calls = _run(
            checkout,
            tmp_path,
            STUB_DRAIN_EXIT="1",
            STUB_STOP_EXIT="1",
            STUB_STOP_STATE=stopped_state,
        )

        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        worker_at = _index_of(calls, lambda a: _is_up(a) and "teatree-worker" in _up_services(a))
        assert proc.returncode != 0
        assert init_at == -1
        assert worker_at == -1
        assert "stop" in proc.stderr

    def test_init_is_aborted_when_the_old_worker_state_is_unreadable(self, checkout: Path, tmp_path: Path) -> None:
        proc, calls = _run(checkout, tmp_path, STUB_WORKER_INSPECT_EXIT="1")

        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode != 0
        assert init_at == -1, "an unreadable old worker must not be mistaken for an absent worker"
        assert "could not determine teatree-worker state" in proc.stderr


class TestAnUnremovableOrphanIsToleratedButOtherFailuresStillAbort:
    """#4822: a Dead orphan the daemon denies exists must not block convergence.

    A container docker ps -a still lists as Dead but the daemon answers
    "No such container" for on inspect/stop/rm — surviving a daemon restart and
    a container prune — makes compose report an error while trying to
    reconcile it as part of a service's up/stop, before the actually-requested
    service is touched. Only that specific daemon response is tolerated; any
    other compose failure still aborts immediately, unchanged.
    """

    def test_an_orphan_on_teatree_init_up_is_ignored_and_the_stack_still_converges(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        proc, calls = _run(checkout, tmp_path, STUB_ORPHAN_UP="teatree-init")

        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode == 0, proc.stderr
        assert init_at != -1, "the tolerated orphan must not stop teatree-init from ever being brought up"
        assert "unremovable orphan" in proc.stderr
        assert "admin + worker are up; stack converged" in proc.stdout

    def test_a_non_orphan_error_on_teatree_init_up_still_aborts_immediately(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        # Control: a generic compose failure with none of the orphan wording
        # must still fail loud — the tolerance is scoped to the one daemon
        # response, not a blanket swallow of every `up` failure.
        proc, calls = _run(checkout, tmp_path, STUB_UP_EXIT="1")

        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode != 0
        assert init_at != -1, "the call must still be attempted"
        assert "unremovable orphan" not in proc.stderr
        assert "FATAL" in proc.stderr

    def test_an_orphan_on_the_worker_stop_is_ignored_when_the_worker_ends_up_contained(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        proc, calls = _run(
            checkout,
            tmp_path,
            STUB_DRAIN_EXIT="1",
            STUB_ORPHAN_STOP="1",
            STUB_STOP_STATE="absent",
        )

        stop_at = _index_of(calls, lambda a: a[:2] == ["stop", "teatree-worker"])
        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode == 0, proc.stderr
        assert stop_at != -1
        assert stop_at < init_at, "the tolerated orphan must not stop containment from proceeding to init"
        assert "unremovable orphan" in proc.stderr
        _assert_fresh_worker_route_precedes_admin_swap(calls, after=init_at)

    def test_a_non_orphan_stop_error_still_aborts_even_when_the_worker_looks_stopped(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        # Same shape as the orphan case above, minus the orphan wording — must
        # still hit the pre-existing FATAL, proving the tolerance is scoped.
        proc, calls = _run(
            checkout,
            tmp_path,
            STUB_DRAIN_EXIT="1",
            STUB_STOP_EXIT="1",
            STUB_STOP_STATE="absent",
        )

        init_at = _index_of(calls, lambda a: _is_up(a) and _up_services(a) == ["teatree-init"])
        assert proc.returncode != 0
        assert init_at == -1
        assert "unremovable orphan" not in proc.stderr
        assert "compose could not stop teatree-worker" in proc.stderr


class TestLogsSurviveTheRecreate:
    def test_each_service_log_is_archived_before_that_service_is_recreated(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        _, calls = _run(checkout, tmp_path)
        for service in _SERVICES.split():
            logs_at = _index_of(calls, lambda a, s=service: a[:1] == ["logs"] and s in a)
            up_at = _index_of(calls, lambda a, s=service: _is_up(a) and s in _up_services(a))
            assert logs_at != -1, f"{service}'s log is destroyed by the recreate with no copy kept"
            assert logs_at < up_at, f"{service}'s log must be archived BEFORE the recreate destroys it"

    def test_the_archive_lands_in_the_bind_mounted_data_dir(self, checkout: Path, tmp_path: Path) -> None:
        _run(checkout, tmp_path)
        archive = tmp_path / "archive"
        assert archive.is_dir(), "no archive directory was created"
        assert sorted(p.name.split("-2")[0] for p in archive.glob("*.log"))


class TestAFailedStageStopsBeforeItCostsAvailability:
    def test_a_failed_init_recreates_no_app_service(self, checkout: Path, tmp_path: Path) -> None:
        proc, calls = _run(checkout, tmp_path, STUB_INIT_STATE="exited 1")
        recreated = {s for c in calls if _is_up(a := _compose_args(c)) for s in _up_services(a)}
        assert proc.returncode != 0
        assert recreated <= {"teatree-init"}, f"a failed init must leave the live generation alone: {recreated!r}"

    def test_a_dashboard_that_does_not_come_back_stops_before_the_worker_is_touched(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        # One answer (the pre-swap sample), none after — the dashboard never returns.
        proc, calls = _run(checkout, tmp_path, STUB_CURL_FAIL_AFTER="1")
        worker_at = _index_of(calls, lambda a: _is_up(a) and "teatree-worker" in _up_services(a))
        assert proc.returncode != 0
        assert worker_at == -1, "with no dashboard answering, swapping the worker leaves no route at all"

    def test_that_abort_leaves_admission_shut_because_init_already_migrated_the_db(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        # Same abort, one stage later in its consequences: init has already migrated the
        # control DB, and the worker still live is the pre-migration one. The EXIT trap
        # must not re-open admission on it. End-to-end through the shipped script, so a
        # fail-safe whose flags were never wired into the convergence reads as the
        # regression it is.
        proc, calls = _run(checkout, tmp_path, STUB_CURL_FAIL_AFTER="1")

        assert proc.returncode != 0
        assert not any("worker_quiescing false" in c for c in calls), (
            f"a convergence stranded after init must leave the gate ON, not admit on a mismatched worker: {calls!r}"
        )
        assert "worker_quiescing" in proc.stderr, "the deliberate refusal must be stated, not silent"


class TestTheResidualWindowIsStated:
    def test_the_dashboard_gap_is_measured_and_reported_with_its_bound(self, checkout: Path, tmp_path: Path) -> None:
        proc, _ = _run(checkout, tmp_path)
        assert "dashboard unavailable for at most" in proc.stdout
        assert "bound 2s" in proc.stdout


class TestTheConvergenceRecordsItsHolder:
    """#4339: the deploy's own in-progress record, written under the flock and cleared on exit.

    ``/proc/locks`` is filtered by pid namespace, so the flock is invisible from the
    watchdog CONTAINER and the crash-loop-fakeable container-age heuristic was all that
    was left. The record crosses that boundary; clearing it is what makes a lock file
    outliving its holder read as not held.
    """

    def test_the_record_is_written_while_the_convergence_runs(self, checkout: Path, tmp_path: Path) -> None:
        snapshot = tmp_path / "lock.snapshot"
        _write_exec(
            checkout / "deploy" / "fast-forward-checkout.sh",
            f'#!/usr/bin/env bash\ncp "$TEATREE_DEPLOY_LOCK" {str(snapshot)!r}\nexit 0\n',
        )

        _run(checkout, tmp_path)

        pid, stamp = snapshot.read_text(encoding="utf-8").split()
        assert pid.isdigit()
        assert stamp.isdigit()

    def test_the_record_is_cleared_on_exit(self, checkout: Path, tmp_path: Path) -> None:
        _run(checkout, tmp_path)

        assert (tmp_path / "deploy.lock").read_text(encoding="utf-8") == "", (
            "a lock file outliving its holder must carry no in-progress record"
        )
