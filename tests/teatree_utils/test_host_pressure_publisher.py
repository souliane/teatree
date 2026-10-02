"""A launchd-owned host feed stays fresh when no statusline is rendered."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from teatree.utils.host_pressure import read_host_pressure

ROOT = Path(__file__).resolve().parents[2]
PUBLISHER = Path(router.__file__).parent / "host-pressure-publish.zsh"
INSTALLER = ROOT / "deploy" / "install-host-pressure.zsh"


def _stub(path: Path, name: str, body: str) -> None:
    script = path / name
    script.write_text(f"#!/bin/zsh\n{body}\n")
    script.chmod(0o755)


def test_publisher_refreshes_host_feed_after_idle_minute(tmp_path: Path) -> None:
    if shutil.which("zsh") is None:
        pytest.skip("host pressure publisher requires zsh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "uname", "print Darwin")
    _stub(
        bin_dir,
        "sysctl",
        "print -r -- '17179869184\n10\n{ 109.45 33.0 20.0 }\ntotal = 3072.00M  used = 1660.00M  free = 1412.00M'",
    )
    _stub(
        bin_dir,
        "vm_stat",
        "print -r -- 'Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
        "Pages free: 100000.\nPages inactive: 21088.'",
    )
    epoch = tmp_path / "epoch"
    epoch.write_text("1000")
    _stub(bin_dir, "date", f"/bin/cat {epoch}")
    env = os.environ | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "T3_LOOP_REGISTRY_DIR": str(tmp_path / "data"),
        "T3_HOST_PRESSURE_MIRROR_DIR": str(tmp_path / "container-host-pressure"),
    }

    first = subprocess.run([str(PUBLISHER)], env=env, capture_output=True, text=True, check=False)
    assert first.returncode == 0, first.stderr
    assert json.loads((tmp_path / "data" / "host-pressure.json").read_text())["epoch"] == 1000

    epoch.write_text("1061")
    second = subprocess.run([str(PUBLISHER)], env=env, capture_output=True, text=True, check=False)
    assert second.returncode == 0, second.stderr
    assert json.loads((tmp_path / "data" / "host-pressure.json").read_text()) == {
        "epoch": 1061,
        "cores": 10,
        "load1": 109.45,
        "ram_available_mib": 473,
        "swap_used_mib": 1660,
        "swap_total_mib": 3072,
    }
    assert (tmp_path / "container-host-pressure" / "host-pressure.json").read_text() == (
        tmp_path / "data" / "host-pressure.json"
    ).read_text()
    assert read_host_pressure(path=tmp_path / "data" / "host-pressure.json", now=1061) is not None


@pytest.mark.parametrize("mirror_kind", ["default", "custom"])
def test_installer_registers_fifteen_second_unattended_launchagent(tmp_path: Path, mirror_kind: str) -> None:
    if shutil.which("zsh") is None:
        pytest.skip("host pressure installer requires zsh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "uname", "print Darwin")
    _stub(bin_dir, "launchctl", 'print -r -- "$*" >> "$TEATREE_TEST_LAUNCH_LOG"')
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    log = tmp_path / "launch.log"
    env = os.environ | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TEATREE_HOST_HOME": str(home),
        "TEATREE_TEST_LAUNCH_LOG": str(log),
    }
    mirror_dir = home / ("custom-pressure" if mirror_kind == "custom" else ".local/share/teatree-host-pressure")
    if mirror_kind == "custom":
        env["T3_HOST_PRESSURE_MIRROR_DIR"] = str(mirror_dir)
    else:
        env.pop("T3_HOST_PRESSURE_MIRROR_DIR", None)

    result = subprocess.run([str(INSTALLER)], env=env, capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    plist = home / "Library" / "LaunchAgents" / "com.teatree.host-pressure.plist"
    content = plist.read_text()
    assert "<integer>15</integer>" in content
    assert "host-pressure-publish.zsh" in content
    assert "/usr/bin:/bin:/usr/sbin:/sbin" in content
    assert str(home / ".local/share/teatree") in content
    assert "T3_HOST_PRESSURE_MIRROR_DIR" in content
    assert str(mirror_dir) in content
    assert mirror_dir.is_dir()
    assert "bootstrap" in log.read_text()


def test_both_setup_and_deploy_converge_the_host_publisher() -> None:
    installer = '"$SCRIPT_DIR/install-host-pressure.zsh"'
    assert installer in (ROOT / "deploy" / "t3").read_text()
    assert installer in (ROOT / "deploy" / "deploy.sh").read_text()


def test_first_remote_rollout_installs_after_checkout_update() -> None:
    deploy = (ROOT / "deploy" / "deploy.sh").read_text()
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert deploy.index('bash "$SCRIPT_DIR/fast-forward-checkout.sh"') < deploy.index(
        '"$SCRIPT_DIR/install-host-pressure.zsh"'
    )
    assert workflow.index("bash deploy/deploy.sh") < workflow.index("deploy/install-host-pressure.zsh")
