"""Setup must not reconcile skills into a stale container's host-mounted agent home."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"


@pytest.mark.parametrize(
    "mount",
    [
        ("bind", "/host_mnt/home/person/.codex", "/home/teatree/.codex", "setup", True),
        ("bind", "/host_mnt/Users/Agent Home/.codex", "/home/teatree/.codex", "setup", True),
        ("bind", "/host_mnt/home/person/.claude/projects", "/home/teatree/.claude/projects", "setup", True),
        ("bind", "/host_mnt/home/person/.agents", "/home/teatree/.agents/skills", "setup", True),
        ("unreadable", "", "/home/teatree/.codex", "setup", True),
        ("volume", "teatree_codex_home", "/home/teatree/.codex", "setup", False),
        ("bind", "/host_mnt/home/person/.codex", "/home/teatree/.codex", "doctor", False),
    ],
)
def test_setup_refuses_a_host_bound_agent_home(tmp_path: Path, mount: tuple[str, str, str, str, bool]) -> None:
    mount_type, source, target, command, refused = mount
    deploy = tmp_path / "checkout" / "deploy"
    deploy.mkdir(parents=True)
    wrapper = deploy / "t3"
    shutil.copy2(WRAPPER, wrapper)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    docker = stub_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = ps ]; then echo 'container-id teatree-admin False'; exit 0; fi\n"
        'if [ "$1" = inspect ]; then\n'
        f"  if [ '{mount_type}' = unreadable ]; then exit 1; fi\n"
        f"  echo '{mount_type} {target} {source}'; exit 0\n"
        "fi\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = exec ]; then echo DISPATCHED; exit 0; fi\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    rc = home / ".zshrc"
    managed_block = (
        "before\n# >>> teatree docker t3 alias >>>\nalias t3=old\n# <<< teatree docker t3 alias <<<\nafter\n"
    )
    rc.write_text(managed_block)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TEATREE_", "T3_"))}
    env["PATH"] = f"{stub_dir}{os.pathsep}{os.defpath}"
    env["TEATREE_HOST_HOME"] = str(home)
    env["GITLAB_TOKEN"] = "unused"

    result = subprocess.run(
        [str(wrapper), command],
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert (result.returncode != 0) is refused
    assert ("DISPATCHED" not in result.stdout) is refused
    if refused:
        assert (target in result.stderr) is (mount_type != "unreadable")
        assert "deploy.sh" in result.stderr
        assert rc.read_text() == managed_block


def test_setup_refuses_when_a_selected_route_loses_its_container_id(tmp_path: Path) -> None:
    deploy = tmp_path / "checkout" / "deploy"
    deploy.mkdir(parents=True)
    wrapper = deploy / "t3"
    source = WRAPPER.read_text()
    wrapper.write_text(
        source.replace(
            'refuse_host_bound_agent_home_on_setup "$@"',
            'RUNNING_SERVICE_TABLE=""\nrefuse_host_bound_agent_home_on_setup "$@"',
            1,
        )
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    docker = stub_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = ps ]; then printf '%s\\n' 'container-id teatree-admin False'; exit 0; fi\n"
        "if [ \"$1\" = inspect ]; then printf '%s\\n' 'volume teatree_codex_home /home/teatree/.codex'; exit 0; fi\n"
        'for arg in "$@"; do if [ "$arg" = exec ]; then printf \'%s\\n\' DISPATCHED; exit 0; fi; done\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TEATREE_", "T3_"))}
    env["PATH"] = f"{stub_dir}{os.pathsep}{os.defpath}"
    env["TEATREE_HOST_HOME"] = str(home)
    env["GITLAB_TOKEN"] = "unused"

    result = subprocess.run([str(wrapper), "setup"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert "cannot verify the running container" in result.stderr
    assert "DISPATCHED" not in result.stdout
