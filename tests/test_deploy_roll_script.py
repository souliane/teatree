# test-path: cross-cutting — drives deploy/roll.sh as a real bash program (no src mirror).
"""``deploy/roll.sh`` builds the generation, then launches the roller from that image under the deploy lock."""

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
BASH = shutil.which("bash") or ""
GIT = shutil.which("git") or ""
FLOCK = shutil.which("flock")


_DOCKER = r"""#!/bin/bash
printf '%s\n' "$*" >>"$FAKE_DIR/docker.log"
case "$1 $2" in
"image inspect") echo "${@: -1}" | sed 's/^.*://' ;;
"info --format") echo /nonexistent ;;
"run --rm")
    case "$*" in
    *ram_probe*) printf 'TEATREE_WORKER_CPUS=2.0\nTEATREE_WORKER_MEM_LIMIT=4g\n' ;;
    *) printf 'x-volumes:\n  - teatree_clones:%s/workspace\n' "${FAKE_CONTAINER_HOME:-/home/teatree}" ;;
    esac ;;
"compose -p")
    {
        printf 'image=%s\n' "$TEATREE_IMAGE"
        printf 'record=%s\n' "$(cat "$TEATREE_DEPLOY_LOCK")"
        printf 'cpus=%s\n' "$TEATREE_WORKER_CPUS"
        printf 'legacy_clone_dir=%s\n' "$TEATREE_LEGACY_CLONE_DIR"
        printf 'watchdog_lock=%s\n' "$TEATREE_WATCHDOG_DEPLOY_LOCK"
        printf 'source_mount=%s\n' "${TEATREE_SOURCE_MOUNT:-}"
    } >>"$FAKE_DIR/roll.env"
    [ ! -x "$FAKE_DIR/roller-hook" ] || "$FAKE_DIR/roller-hook"
    exit "${FAKE_ROLL_RC:-0}" ;;
esac
exit 0
"""


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env)
    return result.stdout.strip()


@dataclass(frozen=True)
class _Fork:
    root: Path
    roll: Path
    sha: str


def _fork(tmp_path: Path, *, nested_core: bool = True, record_refresh_seconds: int = 60) -> _Fork:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    root = tmp_path / ("fork" if nested_core else "teatree")
    deploy = root / "vendor" / "teatree" / "deploy" if nested_core else root / "deploy"
    deploy.mkdir(parents=True)
    for name in ("roll.sh", "build-generation.sh", "generation-topology.sh", "deploy-lock.sh"):
        shutil.copy2(DEPLOY / name, deploy / name)
        (deploy / name).chmod((deploy / name).stat().st_mode | stat.S_IXUSR)
    roll = deploy / "roll.sh"
    assert "\nRECORD_REFRESH_SECONDS=60\n" in roll.read_text(encoding="utf-8")
    roll.write_text(
        roll.read_text(encoding="utf-8").replace(
            "\nRECORD_REFRESH_SECONDS=60\n", f"\nRECORD_REFRESH_SECONDS={record_refresh_seconds}\n"
        ),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("[project]\nname = 'fork'\n", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "origin", "main")
    _git(root, "remote", "set-head", "origin", "main")
    return _Fork(root, deploy / "roll.sh", _git(root, "rev-parse", "HEAD"))


def _roll(tmp_path: Path, fork: _Fork, *args: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    (stubs / "docker").write_text(_DOCKER, encoding="utf-8")
    (stubs / "docker").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TEATREE_", "GIT_", "GITLAB_", "NOTION_"))}
    env |= {
        "PATH": f"{stubs}{os.pathsep}{env['PATH']}",
        "HOME": str(tmp_path / "home"),
        "FAKE_DIR": str(tmp_path),
        "TEATREE_DEPLOY_LOCK": str(tmp_path / "deploy.lock"),
        "TEATREE_HOST_TMP": str(tmp_path),
        **env_extra,
    }
    return subprocess.run([BASH, str(fork.roll), *args], capture_output=True, text=True, env=env, check=False)


def _compose_run(tmp_path: Path) -> str:
    return next(line for line in (tmp_path / "docker.log").read_text().splitlines() if line.startswith("compose"))


def test_rolls_origins_default_branch_tip_from_its_own_image(tmp_path: Path) -> None:
    fork = _fork(tmp_path)

    result = _roll(tmp_path, fork)

    assert result.returncode == 0, result.stderr
    run = _compose_run(tmp_path)
    assert f"--project-directory {fork.roll.parent.resolve()}" in run
    assert re.search(
        rf"run --rm --no-deps {re.escape(_forwarded())} -e TEATREE_DEPLOY_LOCK=/host-tmp/deploy\.lock "
        rf"-e TEATREE_ROLL_RECORD_PID=\d+ teatree-roller deploy roll --to {fork.sha}$",
        run,
    )
    assert f"image=teatree-factory:{fork.sha}" in (tmp_path / "roll.env").read_text()


def _forwarded(*extra: str) -> str:
    names = ["TEATREE_DEPLOY_CHECKOUT", "TEATREE_HOST_HOME", "TEATREE_HOST_OS", "TEATREE_UID", "TEATREE_SOURCE_MOUNT"]
    names += ["TEATREE_DOCKER_SOCKET_GID", "TEATREE_WORKER_CPUS", "TEATREE_WORKER_MEM_LIMIT", "TEATREE_HOST_TMP"]
    names += ["TEATREE_LEGACY_CLONE_DIR", "TEATREE_WATCHDOG_DEPLOY_LOCK", *extra]
    return " ".join(f"-e {name}" for name in names)


def _roll_env(tmp_path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in (tmp_path / "roll.env").read_text().splitlines())


def _compose_files(tmp_path: Path) -> list[str]:
    return [Path(path).name for path in re.findall(r"-f (\S+)", _compose_run(tmp_path))]


def test_the_live_stacks_identity_is_the_default(tmp_path: Path) -> None:
    _roll(tmp_path, _fork(tmp_path))

    assert _compose_run(tmp_path).startswith("compose -p teatree ")
    assert _roll_env(tmp_path)["watchdog_lock"] == "/host-tmp/deploy.lock"


def test_a_second_stack_rolls_under_its_own_project_and_forwards_its_identity(tmp_path: Path) -> None:
    fork = _fork(tmp_path)
    identity = {
        "TEATREE_COMPOSE_PROJECT": "zddproof",
        "TEATREE_PROMOTED_TAG": "zdd:latest",
        "TEATREE_ADMIN_PORT": "8100",
        "TEATREE_IMAGE_REPOSITORY": "registry.example/team/teatree-factory",
    }

    result = _roll(tmp_path, fork, **identity)

    assert result.returncode == 0, result.stderr
    run = _compose_run(tmp_path)
    assert run.startswith("compose -p zddproof ")
    assert _forwarded(*identity) in run
    assert f"image=registry.example/team/teatree-factory:{fork.sha}" in (tmp_path / "roll.env").read_text()


def test_a_lock_the_watchdog_cannot_see_is_refused_before_anything_runs(tmp_path: Path) -> None:
    result = _roll(tmp_path, _fork(tmp_path), TEATREE_HOST_TMP=str(tmp_path / "elsewhere"))

    assert result.returncode == 64
    assert "TEATREE_HOST_TMP" in result.stderr
    assert not (tmp_path / "docker.log").exists()


def test_the_generation_override_joins_the_baked_topology_before_host_identity(tmp_path: Path) -> None:
    _roll(tmp_path, _fork(tmp_path), TEATREE_HOST_HOME=str(tmp_path / "home"))

    assert _compose_files(tmp_path) == [
        "docker-compose.yml",
        "docker-compose.generation.yml",
        "docker-compose.host-identity.yml",
    ]


def test_a_forks_legacy_rollback_is_given_the_vendored_clone_dir_and_the_fork_root(tmp_path: Path) -> None:
    fork = _fork(tmp_path)

    _roll(tmp_path, fork)

    env = _roll_env(tmp_path)
    assert env["legacy_clone_dir"] == "/home/teatree/teatree/vendor/teatree"
    assert env["source_mount"] == str(fork.root.resolve())


def test_a_plain_core_clone_keeps_the_legacy_stack_on_its_named_volume(tmp_path: Path) -> None:
    core = _fork(tmp_path, nested_core=False)

    result = _roll(tmp_path, core)

    assert result.returncode == 0, result.stderr
    env = _roll_env(tmp_path)
    assert env["legacy_clone_dir"] == "/home/teatree/teatree"
    assert env["source_mount"] == ""


def test_every_bind_source_exists_before_the_roller_starts(tmp_path: Path) -> None:
    _roll(tmp_path, _fork(tmp_path))

    home = tmp_path / "home"
    for relative in (".password-store", ".gnupg", ".local/share/teatree", "workspace/t3-workspaces", ".local/bin"):
        assert (home / relative).is_dir(), relative


def test_the_worker_is_sized_by_the_new_images_own_probe(tmp_path: Path) -> None:
    result = _roll(tmp_path, _fork(tmp_path))

    assert "cpus=2.0 mem_limit=4g" in result.stdout
    assert "cpus=2.0" in (tmp_path / "roll.env").read_text()


def test_extra_arguments_reach_the_roller(tmp_path: Path) -> None:
    fork = _fork(tmp_path)

    _roll(tmp_path, fork, "origin/main", "--drain-timeout", "600")

    assert _compose_run(tmp_path).endswith(f"deploy roll --to {fork.sha} --drain-timeout 600")


@pytest.mark.parametrize("roller_exit", [1, 3])
def test_the_rollers_verdict_is_the_scripts_exit_code(tmp_path: Path, roller_exit: int) -> None:
    result = _roll(tmp_path, _fork(tmp_path), FAKE_ROLL_RC=str(roller_exit))

    assert result.returncode == roller_exit


def test_the_deploy_record_the_watchdog_reads_is_held_during_the_roll_and_cleared_after(tmp_path: Path) -> None:
    _roll(tmp_path, _fork(tmp_path))

    record = next(line for line in (tmp_path / "roll.env").read_text().splitlines() if line.startswith("record="))
    pid, heartbeat, deadline = record.removeprefix("record=").split()
    assert pid.isdigit()
    assert int(deadline) > int(heartbeat)
    assert (tmp_path / "deploy.lock").read_text() == ""


def test_a_record_refresh_rewrites_the_record_in_place(tmp_path: Path) -> None:
    # A truncating refresh drops the sentinel; that truncation is the window a reader saw as no record.
    hook = tmp_path / "roller-hook"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        'head -n1 "$TEATREE_DEPLOY_LOCK" >"$FAKE_DIR/first.snapshot"\n'
        "printf 'sentinel\\n' >>\"$TEATREE_DEPLOY_LOCK\"\n"
        'for _ in $(seq 100); do sleep 0.2; [ "$(head -n1 "$TEATREE_DEPLOY_LOCK")" = '
        '"$(cat "$FAKE_DIR/first.snapshot")" ] || break; done\n'
        'cp "$TEATREE_DEPLOY_LOCK" "$FAKE_DIR/after.snapshot"\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)

    result = _roll(tmp_path, _fork(tmp_path, record_refresh_seconds=1))

    assert result.returncode == 0, result.stderr
    pid, heartbeat, deadline = (tmp_path / "first.snapshot").read_text(encoding="utf-8").split()
    record, *rest = (tmp_path / "after.snapshot").read_text(encoding="utf-8").splitlines()
    later_pid, later_heartbeat, later_deadline = record.split()
    assert (later_pid, later_deadline) == (pid, deadline)
    assert int(later_heartbeat) > int(heartbeat)
    assert rest == ["sentinel"], "a refresh must never truncate the record a reader may be reading"


def test_a_deploy_already_in_flight_refuses_with_tempfail(tmp_path: Path) -> None:
    fork = _fork(tmp_path)
    lock = tmp_path / "deploy.lock"
    if FLOCK is None:
        (tmp_path / "deploy.lock.d").mkdir()
        result = _roll(tmp_path, fork)
    else:
        lock.touch()
        holder = subprocess.Popen([FLOCK, str(lock), "-c", "sleep 30"])
        try:
            result = _roll(tmp_path, fork)
        finally:
            holder.kill()
            holder.wait()

    assert result.returncode == 75
    assert not (tmp_path / "roll.env").exists()


def test_the_record_outlasts_the_drain_grace_the_roller_is_given(tmp_path: Path) -> None:
    _roll(tmp_path, _fork(tmp_path), "origin/main", "--drain-timeout", "7200")

    record = next(line for line in (tmp_path / "roll.env").read_text().splitlines() if line.startswith("record="))
    _pid, heartbeat, deadline = record.removeprefix("record=").split()
    assert int(deadline) - int(heartbeat) >= 7200 + 3600


def test_the_roller_is_told_the_pid_its_deploy_record_carries(tmp_path: Path) -> None:
    _roll(tmp_path, _fork(tmp_path))

    record = next(line for line in (tmp_path / "roll.env").read_text().splitlines() if line.startswith("record="))
    forwarded = re.search(r"-e TEATREE_ROLL_RECORD_PID=(\d+) ", _compose_run(tmp_path))
    assert forwarded
    assert record.removeprefix("record=").split()[0] == forwarded.group(1)


def test_a_drain_grace_with_a_leading_zero_is_read_in_base_ten(tmp_path: Path) -> None:
    result = _roll(tmp_path, _fork(tmp_path), "origin/main", "--drain-timeout", "09")

    assert result.returncode == 0, result.stderr
    record = next(line for line in (tmp_path / "roll.env").read_text().splitlines() if line.startswith("record="))
    _pid, heartbeat, deadline = record.removeprefix("record=").split()
    assert 9 + 3600 <= int(deadline) - int(heartbeat) < 1800 + 3600


def test_a_drain_grace_that_is_not_whole_seconds_is_refused(tmp_path: Path) -> None:
    result = _roll(tmp_path, _fork(tmp_path), "origin/main", "--drain-timeout", "1h")

    assert result.returncode == 64
    assert not (tmp_path / "roll.env").exists()


def _without_flock(tmp_path: Path) -> str:
    """Every executable on this PATH except flock, as a host that ships none (macOS) sees it."""
    shelf = tmp_path / "no-flock-bin"
    shelf.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        folder = Path(directory)
        if not folder.is_dir():
            continue
        for tool in folder.iterdir():
            if tool.name != "flock" and not (shelf / tool.name).exists() and os.access(tool, os.X_OK):
                (shelf / tool.name).symlink_to(tool)
    return str(shelf)


def test_a_lock_a_dead_roll_left_behind_is_reclaimed(tmp_path: Path) -> None:
    fork = _fork(tmp_path)
    finished = subprocess.Popen([shutil.which("true") or "/usr/bin/true"])
    finished.wait()
    lock_dir = tmp_path / "deploy.lock.d"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(f"{finished.pid}\n", encoding="utf-8")

    result = _roll(tmp_path, fork, PATH=f"{tmp_path / 'bin'}{os.pathsep}{_without_flock(tmp_path)}")

    assert result.returncode == 0, result.stderr
    assert f"reclaiming {lock_dir} from dead pid {finished.pid}" in result.stderr
    assert not lock_dir.exists()


def test_a_checkout_at_the_tree_the_image_bakes_is_refused(tmp_path: Path) -> None:
    core = _fork(tmp_path, nested_core=False)

    result = _roll(tmp_path, core, FAKE_CONTAINER_HOME=str(tmp_path.resolve()))

    assert result.returncode == 64
    assert "the tree the image bakes" in result.stderr
    assert not (tmp_path / "roll.env").exists()
