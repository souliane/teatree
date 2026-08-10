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
if [ "$1" = inspect ]; then
    printf '%s\\n' "${{STUB_INIT_STATE:-exited 0}}"
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
    *"worker status"*) printf '{{"running": true}}\\n' ;;
    esac
    exit "${{STUB_EXEC_EXIT:-0}}"
    ;;
ps)
    case "$*" in
    *--quiet*) printf '%s\\n' "${{STUB_CONTAINER_ID:-stubcid}}" ;;
    esac
    exit 0
    ;;
config)
    printf '%s\\n' {" ".join(_SERVICES.split())}
    exit 0
    ;;
build) exit "${{STUB_BUILD_EXIT:-0}}" ;;
up) exit "${{STUB_UP_EXIT:-0}}" ;;
esac
exit 0
"""

# Answers until STUB_CURL_FAIL_AFTER calls have been served, so a test can model a
# dashboard that was up before its swap and never came back after it.
_CURL_STUB = """#!/usr/bin/env bash
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
        STUB_DOCKER_LOG=str(docker_log),
        STUB_CURL_COUNT=str(tmp_path / "curl.count"),
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
        inspected_at = next((i for i, c in enumerate(calls) if c.startswith("inspect ")), -1)
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
