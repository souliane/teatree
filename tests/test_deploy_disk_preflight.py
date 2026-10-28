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


@pytest.mark.parametrize(("free_gib", "allowed"), [(16, False), (20, True)])
def test_deploy_reclaims_then_checks_space_before_build(tmp_path: Path, free_gib: int, *, allowed: bool) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index('. "$SCRIPT_DIR/disk-preflight.sh"')
    end = source.index("# Fetch now, fast-forward later:", start)
    trace = tmp_path / "trace"
    script = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR={DEPLOY.parent}\n"
        f"TRACE={trace}\n"
        'compose() { printf \'compose %s\\n\' "$*" >>"$TRACE"; }\n'
        'docker() { printf \'docker %s\\n\' "$*" >>"$TRACE"; }\n'
        "df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n"
        f"/dev/fake 40000000 1000000 {free_gib * 1048576} 1%% /\\n'; }}\n" + source[start:end] + "compose build\n"
    )

    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True, check=False, env=os.environ.copy())

    assert (result.returncode == 0) is allowed
    assert "compose exec -T teatree-worker" in trace.read_text(encoding="utf-8")
    assert ("compose build" in trace.read_text(encoding="utf-8")) is allowed
    assert f"{free_gib}.0 GiB free" in result.stdout + result.stderr
    assert "reserved" in result.stdout + result.stderr
    assert "17.0 GiB" in result.stdout + result.stderr


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
