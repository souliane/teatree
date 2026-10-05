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


def _publisher_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    if shutil.which("zsh") is None:
        pytest.skip("host pressure publisher requires zsh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    for name, value in {"epoch": "1000", "swapins": "100", "swapouts": "50", "level": "1"}.items():
        (state / name).write_text(value)
    _stub(bin_dir, "uname", "print Darwin")
    _stub(
        bin_dir,
        "sysctl",
        f"""case "$*" in
    *pressure_level*) /bin/cat {state}/level ;;
    *) print -r -- '17179869184\n10\n{{ 109.45 33.0 20.0 }}\ntotal = 3072.00M  used = 1660.00M  free = 1412.00M' ;;
esac""",
    )
    _stub(
        bin_dir,
        "vm_stat",
        "\n".join(
            (
                "print -r -- 'Mach Virtual Memory Statistics: (page size of 16384 bytes)'",
                "print -r -- 'Pages free: 25000.'",
                "print -r -- 'Pages inactive: 5272.'",
                f'print -r -- "Swapins: $(/bin/cat {state}/swapins)."',
                f'print -r -- "Swapouts: $(/bin/cat {state}/swapouts)."',
            )
        ),
    )
    _stub(bin_dir, "date", f"/bin/cat {state}/epoch")
    env = os.environ | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "T3_LOOP_REGISTRY_DIR": str(tmp_path / "data"),
        "T3_HOST_PRESSURE_MIRROR_DIR": str(tmp_path / "container-host-pressure"),
    }
    return env, state


def _publish(env: dict[str, str], tmp_path: Path) -> dict[str, object]:
    result = subprocess.run([str(PUBLISHER)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads((tmp_path / "data" / "host-pressure.json").read_text())


def test_publisher_refreshes_host_feed_after_idle_minute(tmp_path: Path) -> None:
    env, state = _publisher_env(tmp_path)

    assert _publish(env, tmp_path)["epoch"] == 1000

    (state / "epoch").write_text("1061")
    assert _publish(env, tmp_path) == {
        "epoch": 1061,
        "cores": 10,
        "load1": 109.45,
        "ram_available_mib": 473,
        "swap_used_mib": 1660,
        "swap_total_mib": 3072,
        "swap_pages": 150,
        "vm_pressure_level": 1,
    }
    assert (tmp_path / "container-host-pressure" / "host-pressure.json").read_text() == (
        tmp_path / "data" / "host-pressure.json"
    ).read_text()
    assert read_host_pressure(path=tmp_path / "data" / "host-pressure.json", now=1061) is not None


def test_publisher_reports_swap_activity_between_consecutive_samples(tmp_path: Path) -> None:
    env, state = _publisher_env(tmp_path)
    _publish(env, tmp_path)

    # 9216 pages of 16 KiB in 15 s is 144 MiB, or 9.6 MiB/s.
    (state / "epoch").write_text("1015")
    (state / "swapouts").write_text(str(50 + 9216))
    (state / "level").write_text("2")
    feed = _publish(env, tmp_path)

    assert feed["swap_mib_per_s"] == pytest.approx(9.6)
    assert feed["vm_pressure_level"] == 2
    reading = read_host_pressure(path=tmp_path / "data" / "host-pressure.json", now=1015)
    assert reading is not None
    assert reading.swap_mib_per_s == pytest.approx(9.6)


def test_a_comma_decimal_locale_still_publishes_valid_json(tmp_path: Path) -> None:
    env, state = _publisher_env(tmp_path)
    env |= {"LC_ALL": "de_AT.UTF-8", "LANG": "de_AT.UTF-8"}
    _publish(env, tmp_path)

    (state / "epoch").write_text("1015")
    (state / "swapouts").write_text(str(50 + 9216))

    assert _publish(env, tmp_path)["swap_mib_per_s"] == pytest.approx(9.6)


@pytest.mark.parametrize(("next_epoch", "next_swapins"), [(1075, "200"), (1015, "10")], ids=["gap", "counter-reset"])
def test_publisher_omits_the_rate_it_cannot_measure(tmp_path: Path, next_epoch: int, next_swapins: str) -> None:
    env, state = _publisher_env(tmp_path)
    _publish(env, tmp_path)

    (state / "epoch").write_text(str(next_epoch))
    (state / "swapins").write_text(next_swapins)

    assert "swap_mib_per_s" not in _publish(env, tmp_path)


def test_publisher_omits_a_pressure_level_the_kernel_does_not_report(tmp_path: Path) -> None:
    env, state = _publisher_env(tmp_path)
    (state / "level").write_text("")

    assert "vm_pressure_level" not in _publish(env, tmp_path)


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
    assert deploy.count('"$SCRIPT_DIR/install-host-pressure.zsh"') == 1
    assert deploy.index('bash "$SCRIPT_DIR/fast-forward-checkout.sh"') < deploy.index(
        '"$SCRIPT_DIR/install-host-pressure.zsh"'
    )
    assert workflow.count("deploy/install-host-pressure.zsh") == 1
    assert workflow.index("bash deploy/deploy.sh") < workflow.index("deploy/install-host-pressure.zsh")
