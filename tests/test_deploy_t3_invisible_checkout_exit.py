# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""``deploy/t3`` answers "this venue cannot run t3 here" with exit 69, never the CLI's own 1.

The no-orphan pre-push hook runs the installed ``t3`` and must tell a venue that cannot
run it (skip, warn, let the push through) from a CLI that ran and refused (fail the
push). Every wrapper-side refusal on that path used to exit 1, the CLI's own refusal
code, so the hook could not branch on it. Exit 75 (mid-update) and 127 (no docker) already
had their own codes. The hook also needs prek's pushed ref inside the container: a push from a
detached HEAD names its branch only there.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from _deploy_wrapper_paths import container_credential_prologue, copy_wrapper

VENUE_UNAVAILABLE = 69
WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
SERVICE_IMAGE = "teatree-worker:local"

DOCKER_STUB = """#!/usr/bin/env bash
case "$1" in
version) exit "${STUB_DAEMON_EXIT:-0}" ;;
image) exit "${STUB_IMAGE_EXIT:-0}" ;;
esac
for arg in "$@"; do
    case "$arg" in
    ps) exit 0 ;;
    config) printf '%s\\n' "${STUB_SERVICE_IMAGE:-}"; exit 0 ;;
    esac
done
printf 'DISPATCHED\\n'
"""

WEDGED_PASS_STUB = """#!/bin/sh
echo "gpg: decryption failed: No Keybox daemon running" >&2
exit 2
"""


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    host_home = tmp_path / "home"
    host_home.mkdir(exist_ok=True)
    return host_home


def _run_wrapper(
    tmp_path: Path, home: Path, *, cwd: Path | None = None, args: tuple[str, ...] = ("--help",), **env_extra: str
) -> subprocess.CompletedProcess[str]:
    entry = tmp_path / "fork" / "vendor" / "teatree" / "deploy" / "t3"
    entry.parent.mkdir(parents=True)
    copy_wrapper(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "fork" / "pyproject.toml").write_text('[project]\nname = "fork"\n', encoding="utf-8")
    _stub(home / "stub-bin" / "docker", DOCKER_STUB)
    plain = tmp_path / "elsewhere"
    plain.mkdir(exist_ok=True)

    env = {k: v for k, v in os.environ.items() if k not in {"TEATREE_SOURCE_MOUNT", "TEATREE_INVOCATION_CWD"}}
    env["PATH"] = f"{home / 'stub-bin'}{os.pathsep}{env['PATH']}"
    env |= {"TEATREE_HOST_HOME": str(home), "GITLAB_TOKEN": "unused", "STUB_SERVICE_IMAGE": SERVICE_IMAGE} | env_extra
    return subprocess.run(
        [str(entry), *args], capture_output=True, text=True, env=env, cwd=cwd or plain, check=False, timeout=120
    )


def test_exit_69_for_a_checkout_outside_every_translatable_root(tmp_path: Path, home: Path) -> None:
    checkout = tmp_path / "host-only-clone"
    (checkout / ".git").mkdir(parents=True)

    proc = _run_wrapper(tmp_path, home, cwd=checkout)

    assert proc.returncode == VENUE_UNAVAILABLE, proc.stderr
    assert "not visible inside the container" in proc.stderr


def test_exit_0_for_a_checkout_inside_a_translatable_root(tmp_path: Path, home: Path) -> None:
    checkout = home / "workspace" / "t3-workspaces" / "1234-ticket" / "teatree"
    (checkout / ".git").mkdir(parents=True)

    proc = _run_wrapper(tmp_path, home, cwd=checkout)

    assert proc.returncode == 0, proc.stderr
    assert "DISPATCHED" in proc.stdout


@pytest.mark.parametrize(
    ("stub_env", "named"),
    [({"STUB_DAEMON_EXIT": "1"}, "Docker daemon"), ({"STUB_IMAGE_EXIT": "1"}, SERVICE_IMAGE)],
    ids=["daemon-unreachable", "image-never-built"],
)
def test_exit_69_for_an_absent_stack(tmp_path: Path, home: Path, stub_env: dict[str, str], named: str) -> None:
    proc = _run_wrapper(tmp_path, home, **stub_env)

    assert proc.returncode == VENUE_UNAVAILABLE, proc.stderr
    assert named in proc.stderr


def test_exit_69_for_a_wedged_host_secret_store(tmp_path: Path, home: Path) -> None:
    _stub(home / "stub-bin" / "pass", WEDGED_PASS_STUB)

    proc = _run_wrapper(tmp_path, home, args=("info",), TEATREE_GITLAB_TOKEN_PASS_PATH="gitlab/pat", GITLAB_TOKEN="")

    assert proc.returncode == VENUE_UNAVAILABLE, proc.stderr
    assert "WEDGED" in proc.stderr


@pytest.mark.skipif(shutil.which("timeout") is None, reason="the wedge is only observable where the read is bounded")
def test_exit_69_for_a_wedged_container_secret_store(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _stub(bin_dir / "pass", WEDGED_PASS_STUB)
    for tool in ("head", "timeout"):
        (bin_dir / tool).symlink_to(shutil.which(tool) or tool)

    proc = subprocess.run(
        [shutil.which("sh") or "sh", "-c", container_credential_prologue(), "t3-in-container", "true"],
        capture_output=True,
        text=True,
        env={
            "PATH": str(bin_dir),
            "TEATREE_GNUPG_RUNTIME_DIR": str(tmp_path / "run"),
            "TEATREE_GITLAB_TOKEN_PASS_PATH": "gitlab/pat",
        },
        check=False,
        timeout=120,
    )

    assert proc.returncode == VENUE_UNAVAILABLE, proc.stderr
    assert "WEDGED" in proc.stderr


def test_the_ref_prek_pushes_crosses_into_the_container() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    start = source.index("FORWARD_ENV_NAMES=(")
    forwarded = source[start : source.index("\n)\n", start)].splitlines()

    assert "    PRE_COMMIT_REMOTE_BRANCH" in forwarded
