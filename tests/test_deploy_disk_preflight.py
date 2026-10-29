"""The deployed shell refuses a build when its filesystem cannot support one."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"
BASH = shutil.which("bash") or "/bin/bash"


def test_unmounted_docker_root_does_not_refuse_a_measurable_host_build(tmp_path: Path) -> None:
    trace = tmp_path / "trace"
    script = (
        "set -euo pipefail\n"
        f"TRACE={trace}\n"
        'docker() { printf "%s\\n" /unmounted-docker-root; }\n'
        'df() { if [ "$2" = /unmounted-docker-root ]; then return 1; fi; '
        'printf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
        '/dev/fake 40000000 1000000 20971520 1%% /\\n"; }\n'
        f'. "{DEPLOY.parent / "disk-preflight.sh"}"\n'
        'require_build_space "deploy"\n'
        'printf "built\\n" >"$TRACE"\n'
    )

    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert trace.read_text(encoding="utf-8") == "built\n"
    assert "20.0 GiB free" in result.stdout


def test_preflight_ignores_small_tmpfs_when_disk_has_room() -> None:
    script = (
        "set -euo pipefail\n"
        "TMPDIR=/tmp\n"
        'docker() { printf "%s\\n" /var/lib/docker; }\n'
        'df() { case "$2" in '
        "/) source=/dev/fake; free=24117248;; "
        "/tmp) source=tmpfs; free=3670016;; "
        "/var/lib/docker) source=/dev/fake; free=20971520;; "
        'esac; printf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
        '%s 40000000 1000000 %s 1%% %s\\n" "$source" "$free" "$2"; }\n'
        f'. "{DEPLOY.parent / "disk-preflight.sh"}"\n'
        'require_build_space "deploy"\n'
    )

    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "20.0 GiB free" in result.stdout
    assert "at /var/lib/docker" in result.stdout


def test_deploy_workflow_extracts_build_context_on_disk() -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"

    assert 'cd "$HOME/teatree-deploy" && TMPDIR=/var/tmp bash deploy/deploy.sh' in workflow.read_text(encoding="utf-8")


def test_preflight_refuses_when_no_host_path_can_be_measured() -> None:
    script = (
        "set -euo pipefail\n"
        'docker() { printf "%s\\n" /unmounted-docker-root; }\n'
        "df() { return 1; }\n"
        f'. "{DEPLOY.parent / "disk-preflight.sh"}"\n'
        'require_build_space "deploy"\n'
    )

    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert "could not be measured" in result.stderr


def test_preflight_refuses_when_root_cannot_be_measured() -> None:
    script = (
        "set -euo pipefail\n"
        'docker() { printf "%s\\n" /unmounted-docker-root; }\n'
        'df() { if [ "$2" = / ]; then return 1; fi; '
        'printf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
        '/dev/fake 40000000 1000000 20971520 1%% /\\n"; }\n'
        f'. "{DEPLOY.parent / "disk-preflight.sh"}"\n'
        'require_build_space "deploy"\n'
    )

    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert "could not be measured" in result.stderr


WORKER_RECLAIM = "compose exec -T teatree-worker t3 worker reclaim --budget-seconds 90"


def _df_stub(free_gib: int | None, after_reclaim_gib: int | None) -> str:
    """A ``df`` that reports *free_gib* until a reclaim command touches ``$FREED``, then *after_reclaim_gib*."""
    if free_gib is None:
        return "df() { return 1; }\n"
    before_kib, after_kib = free_gib * 1048576, (after_reclaim_gib or 0) * 1048576
    header = "Filesystem 1024-blocks Used Available Capacity Mounted on"
    return (
        f'df() {{ if [ -e "$FREED" ]; then a={after_kib}; else a={before_kib}; fi; '
        f"printf '{header}\\n/dev/fake 40000000 1000000 %s 1%% /\\n' \"$a\"; }}\n"
    )


def _run_preflight_slice(
    tmp_path: Path,
    *,
    free_gib: int | None,
    free_gib_after_reclaim: int | None = None,
    worker_reclaim_works: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index('. "$SCRIPT_DIR/disk-preflight.sh"')
    end = source.index("# Fetch now, fast-forward later:", start)
    trace = tmp_path / "trace"
    after = free_gib if free_gib_after_reclaim is None else free_gib_after_reclaim
    worker_exit = "return 0" if worker_reclaim_works else "return 1"
    script = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR={DEPLOY.parent}\nTRACE={trace}\nFREED={tmp_path / 'freed'}\n"
        'compose() { printf "compose %s\\n" "$*" >>"$TRACE"; '
        f'if [ "$1" = exec ]; then touch "$FREED"; {worker_exit}; fi; }}\n'
        'docker() { case "$1" in info) return 0;; esac; printf "docker %s\\n" "$*" >>"$TRACE"; touch "$FREED"; }\n'
        + _df_stub(free_gib, after)
        + source[start:end]
        + "compose build\n"
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False, env=os.environ.copy())
    return result, trace.read_text(encoding="utf-8").splitlines() if trace.exists() else []


def test_a_box_with_room_for_the_build_skips_the_reclaim_and_says_so(tmp_path: Path) -> None:
    result, trace = _run_preflight_slice(tmp_path, free_gib=20)

    assert result.returncode == 0, result.stderr
    assert trace == ["compose build"]
    assert "skipping the pre-build reclaim" in result.stdout
    assert "20.0 GiB free" in result.stdout


def test_a_box_short_of_the_build_need_reclaims_then_rechecks_then_builds(tmp_path: Path) -> None:
    result, trace = _run_preflight_slice(tmp_path, free_gib=16, free_gib_after_reclaim=20)

    assert result.returncode == 0, result.stderr
    assert trace == [WORKER_RECLAIM, "compose build"]
    assert "reclaiming disk before the build" in result.stdout
    assert "20.0 GiB free" in result.stdout


def test_a_box_that_stays_short_after_the_reclaim_refuses_to_build(tmp_path: Path) -> None:
    result, trace = _run_preflight_slice(tmp_path, free_gib=16)

    assert result.returncode != 0
    assert trace == [WORKER_RECLAIM]
    assert "NOT BUILDING" in result.stderr
    assert "16.0 GiB free" in result.stderr
    assert "17.0 GiB" in result.stderr


def test_deploy_uses_prune_fallback_when_worker_reclaim_fails(tmp_path: Path) -> None:
    result, trace = _run_preflight_slice(tmp_path, free_gib=16, free_gib_after_reclaim=20, worker_reclaim_works=False)

    assert result.returncode == 0, result.stderr
    assert trace == [WORKER_RECLAIM, "docker builder prune -f", "docker image prune -f", "compose build"]


def test_a_box_whose_space_cannot_be_measured_reclaims_nothing_and_refuses(tmp_path: Path) -> None:
    result, trace = _run_preflight_slice(tmp_path, free_gib=None)

    assert result.returncode != 0
    assert trace == []
    assert "could not be measured" in result.stderr


def test_deploy_low_space_log_keeps_the_preflight_error() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index('. "$SCRIPT_DIR/disk-preflight.sh"')
    end = source.index("# Fetch now, fast-forward later:", start)
    script = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR={DEPLOY.parent}\n"
        'compose() { :; }\ndocker() { printf "%s\\n" /; }\n'
        "df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n"
        "/dev/fake 40000000 1000 524288 1%% /\\n'; }\n" + source[start:end]
    )

    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode != 0
    log_line = next(line for line in result.stdout.splitlines() if line.startswith("deploy: low-space log: "))
    log = Path(log_line.removeprefix("deploy: low-space log: "))
    try:
        assert log.parent == Path("/dev/shm")
        assert "reserved" in log.read_text(encoding="utf-8")
        assert "17.0 GiB" in log.read_text(encoding="utf-8")
    finally:
        log.unlink(missing_ok=True)


@pytest.mark.parametrize(("free_gib", "allowed"), [(16, False), (20, True)])
def test_staged_build_rechecks_space_after_archive_staging(tmp_path: Path, free_gib: int, *, allowed: bool) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index("staged_swap() {")
    end = source.index("\n}", start) + 2
    trace = tmp_path / "trace"
    script = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR={DEPLOY.parent}\n"
        f"TRACE={trace}\n"
        'compose() { printf \'%s\\n\' "$*" >>"$TRACE"; }\n'
        "BUILD_CONTEXT=/fake\n_WORKER_CONTAINED=false\n"
        "build_ro_executor() { :; }\n"
        "drain_worker() { :; }\nfast_forward_live_tree() { :; }\n"
        "archive_service_logs() { :; }\nwait_for_init() { :; }\n"
        "recreate_ro_executor() { :; }\nswap_admin() { :; }\n"
        "resume_admission() { :; }\nremaining_services() { :; }\n"
        "df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n"
        f"/dev/fake 40000000 1000000 {free_gib * 1048576} 1%% /\\n'; }}\n"
        '. "$SCRIPT_DIR/disk-preflight.sh"\n' + source[start:end] + "\nstaged_swap\n"
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False)

    assert (result.returncode == 0) is allowed
    assert (trace.exists() and "build" in trace.read_text(encoding="utf-8")) is allowed
    if not allowed:
        assert "16.0 GiB free" in result.stderr


@pytest.mark.parametrize(("free_gib", "allowed"), [(16, False), (20, True)])
def test_executor_image_build_rechecks_space(tmp_path: Path, free_gib: int, *, allowed: bool) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index("build_ro_executor() {")
    end = source.index("\n}", start) + 2
    trace = tmp_path / "trace"
    script = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR={DEPLOY.parent}\n"
        f"TRACE={trace}\n"
        "BUILD_CONTEXT=/fake\nEXECUTOR_SERVICE=executor\n_EXECUTOR_FAILURE=\n"
        'compose() { printf \'%s\\n\' "$*" >>"$TRACE"; }\n'
        "df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n"
        f"/dev/fake 40000000 1000000 {free_gib * 1048576} 1%% /\\n'; }}\n"
        '. "$SCRIPT_DIR/disk-preflight.sh"\n' + source[start:end] + "\nbuild_ro_executor\n"
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert (trace.exists() and "build executor" in trace.read_text(encoding="utf-8")) is allowed
    if not allowed:
        assert "16.0 GiB free" in result.stderr
