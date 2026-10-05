# test-path: cross-cutting — pins deploy/docker-compose*.yml and the scripts reading them; no src mirror.
"""An image generation runs its baked tree through the generation override; the default compose stays source-mounted."""

import json
import os
import pwd
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
BASE = DEPLOY / "docker-compose.yml"
GENERATION = DEPLOY / "docker-compose.generation.yml"
HOST_IDENTITY = DEPLOY / "docker-compose.host-identity.yml"
ROLES = ("teatree-init", "teatree-worker", "teatree-admin", "teatree-slack-listener")
RUNTIME_SERVICES = (*ROLES, "teatree-watchdog")
SOURCE_TARGET = "/home/teatree/teatree"
UV_VOLUME_TARGET = "/opt/teatree/uv"
BAKED_ENTRYPOINT = ["/usr/local/bin/entrypoint.sh"]
BASH = shutil.which("bash") or ""


class _ComposeLoader(yaml.SafeLoader):
    """Reads compose's merge tags the way compose applies them: `!reset` drops the key, `!override` keeps the value."""


class _Reset:
    pass


_ComposeLoader.add_constructor("!reset", lambda _loader, _node: _Reset())
_ComposeLoader.add_constructor(
    "!override",
    lambda loader, node: (
        loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node, deep=True)
    ),
)


def _load(path: Path) -> dict:
    loader = _ComposeLoader(path.read_text(encoding="utf-8"))
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _target(volume: str | dict) -> str:
    if isinstance(volume, dict):
        return volume["target"]
    return re.sub(r"\$\{[^}]*\}", "_", volume).split(":")[1]


class TestTheOverrideMirrorsTheBaseVolumes:
    def test_it_is_the_base_volume_list_minus_the_source_and_its_install(self) -> None:
        base = _load(BASE)["x-teatree-common"]["volumes"]
        expected = [volume for volume in base if _target(volume) not in {SOURCE_TARGET, UV_VOLUME_TARGET}]

        assert _load(GENERATION)["x-generation-volumes"] == expected

    def test_the_base_still_mounts_both(self) -> None:
        targets = [_target(volume) for volume in _load(BASE)["x-teatree-common"]["volumes"]]

        assert SOURCE_TARGET in targets
        assert UV_VOLUME_TARGET in targets


@pytest.mark.parametrize("name", RUNTIME_SERVICES)
class TestTheOverrideDropsEverythingTheCheckoutSupplies:
    def test_the_build_is_reset(self, name: str) -> None:
        assert isinstance(_load(GENERATION)["services"][name]["build"], _Reset)

    def test_the_image_is_never_pulled(self, name: str) -> None:
        assert _load(GENERATION)["services"][name]["pull_policy"] == "never"

    def test_the_entrypoint_is_the_images_own(self, name: str) -> None:
        assert _load(GENERATION)["services"][name]["entrypoint"] == BAKED_ENTRYPOINT

    def test_the_clone_dir_is_left_to_the_image(self, name: str) -> None:
        assert isinstance(_load(GENERATION)["services"][name]["environment"]["TEATREE_CLONE_DIR"], _Reset)


def test_the_roller_is_a_one_shot_that_cannot_write_the_checkout() -> None:
    roller = _load(GENERATION)["services"]["teatree-roller"]

    assert roller["profiles"] == ["roll"]
    checkout = next(v for v in roller["volumes"] if isinstance(v, dict) and "TEATREE_DEPLOY_CHECKOUT" in v["target"])
    assert checkout["read_only"] is True
    assert "/var/run/docker.sock:/var/run/docker.sock" in roller["volumes"]


def test_the_default_compose_never_names_the_roller_or_the_generation_layout() -> None:
    assert "teatree-roller" not in _load(BASE)["services"]
    assert "pull_policy" not in _load(BASE)["x-teatree-common"]


def _render(tmp_path: Path, *files: Path, **env_overrides: str) -> dict:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("needs the docker CLI to render the compose config (no daemon required)")
    for source in files:
        (tmp_path / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TEATREE_")}
    env["DOCKER_CONFIG"] = str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".docker")
    env.update(env_overrides)
    argv = [docker, "compose", "--profile", "roll", *(a for f in files for a in ("-f", str(tmp_path / f.name)))]
    proc = subprocess.run([*argv, "config", "--format", "json"], capture_output=True, text=True, env=env, check=False)
    if proc.returncode != 0 and "unknown" in proc.stderr and "flag" in proc.stderr:
        pytest.skip(f"docker CLI lacks the compose plugin: {proc.stderr.strip()}")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["services"]


def _rendered_targets(service: dict) -> list[str]:
    return [volume["target"] for volume in service.get("volumes") or []]


class TestTheRenderedGenerationStack:
    def test_no_role_mounts_anything_over_the_baked_source_or_its_install(self, tmp_path: Path) -> None:
        services = _render(tmp_path, BASE, GENERATION, TEATREE_IMAGE="teatree-factory:" + "a" * 40)

        for name in RUNTIME_SERVICES:
            targets = _rendered_targets(services[name])
            assert SOURCE_TARGET not in targets, name
            assert UV_VOLUME_TARGET not in targets, name
            assert services[name]["entrypoint"] == BAKED_ENTRYPOINT, name
            assert "build" not in services[name], name
            assert "TEATREE_CLONE_DIR" not in services[name]["environment"], name
            assert services[name]["image"] == "teatree-factory:" + "a" * 40, name

    def test_every_other_mount_of_the_default_stack_survives(self, tmp_path: Path) -> None:
        base = _render(tmp_path, BASE)
        generation = _render(tmp_path, BASE, GENERATION)

        for name in ROLES:
            kept = [t for t in _rendered_targets(base[name]) if t not in {SOURCE_TARGET, UV_VOLUME_TARGET}]
            assert _rendered_targets(generation[name]) == kept, name

    def test_host_identity_still_merges_on_top_of_the_override(self, tmp_path: Path) -> None:
        home = "/Users/someone"
        services = _render(tmp_path, BASE, GENERATION, HOST_IDENTITY, TEATREE_HOST_HOME=home)

        assert f"{home}/workspace" in _rendered_targets(services["teatree-worker"])

    def test_a_second_stack_gets_its_own_admin_port_and_watchdog_identity(self, tmp_path: Path) -> None:
        services = _render(
            tmp_path,
            BASE,
            GENERATION,
            TEATREE_ADMIN_PORT="8100",
            TEATREE_COMPOSE_PROJECT="zddproof",
            TEATREE_WATCHDOG_DEPLOY_LOCK="/host-tmp/zddproof.lock",
        )

        assert services["teatree-admin"]["environment"]["TEATREE_ADMIN_PORT"] == "8100"
        watchdog = services["teatree-watchdog"]["environment"]
        assert watchdog["TEATREE_WATCHDOG_PROJECT"] == "zddproof"
        assert watchdog["TEATREE_WATCHDOG_DEPLOY_LOCK"] == "/host-tmp/zddproof.lock"
        assert watchdog["TEATREE_ADMIN_PORT"] == "8100"

    def test_the_defaults_are_the_live_stacks(self, tmp_path: Path) -> None:
        services = _render(tmp_path, BASE, GENERATION)

        assert services["teatree-admin"]["environment"]["TEATREE_ADMIN_PORT"] == "8000"
        watchdog = services["teatree-watchdog"]["environment"]
        assert watchdog["TEATREE_WATCHDOG_PROJECT"] == "teatree"
        assert watchdog["TEATREE_WATCHDOG_DEPLOY_LOCK"] == "/host-tmp/teatree-deploy.lock"


def _host_tmp_source(services: dict) -> str:
    return next(v["source"] for v in services["teatree-watchdog"]["volumes"] if v["target"] == "/host-tmp")


class TestTheWatchdogSeesTheLockWhereRollShPutsIt:
    def test_the_live_stack_still_binds_the_host_tmp(self, tmp_path: Path) -> None:
        assert _host_tmp_source(_render(tmp_path, BASE)) == "/tmp"

    def test_a_stack_with_its_own_host_tmp_binds_that(self, tmp_path: Path) -> None:
        services = _render(tmp_path, BASE, GENERATION, TEATREE_HOST_TMP="/srv/zddproof-tmp")

        assert _host_tmp_source(services) == "/srv/zddproof-tmp"
        assert services["teatree-watchdog"]["environment"]["TEATREE_HOST_TMP"] == "/srv/zddproof-tmp"


class TestTheWatchdogRepairsInItsOwnLayout:
    def _compose_argv(self, tmp_path: Path, **env: str) -> str:
        stubs = tmp_path / "bin"
        stubs.mkdir(exist_ok=True)
        docker = stubs / "docker"
        docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >>"$FAKE_LOG"\n', encoding="utf-8")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        log = tmp_path / "docker.log"
        harness = tmp_path / "harness.sh"
        harness.write_text(f'source "{DEPLOY / "watchdog.sh"}"\ncompose ps\n', encoding="utf-8")
        run_env = {k: v for k, v in os.environ.items() if not k.startswith("TEATREE_")}
        run_env |= {"PATH": f"{stubs}{os.pathsep}{run_env['PATH']}", "FAKE_LOG": str(log), **env}
        subprocess.run([BASH, str(harness)], capture_output=True, text=True, env=run_env, check=False)
        return log.read_text(encoding="utf-8").strip()

    def test_an_image_generation_adds_its_baked_generation_override(self, tmp_path: Path) -> None:
        argv = self._compose_argv(tmp_path, TEATREE_GENERATION="a" * 40)

        assert argv == f"compose -p teatree -f {BASE} -f {GENERATION} ps"

    def test_a_legacy_watchdog_repairs_with_the_default_compose_alone(self, tmp_path: Path) -> None:
        assert self._compose_argv(tmp_path) == f"compose -p teatree -f {BASE} ps"

    def test_a_second_stacks_watchdog_repairs_its_own_project(self, tmp_path: Path) -> None:
        argv = self._compose_argv(tmp_path, TEATREE_GENERATION="a" * 40, TEATREE_WATCHDOG_PROJECT="zddproof")

        assert argv.startswith("compose -p zddproof ")

    def test_the_shared_project_setting_wins_for_the_watchdog(self, tmp_path: Path) -> None:
        argv = self._compose_argv(tmp_path, TEATREE_COMPOSE_PROJECT="zddproof", TEATREE_WATCHDOG_PROJECT="teatree")

        assert argv.startswith("compose -p zddproof ")

    def test_missing_off_box_worker_is_recreated_with_host_identity(self, tmp_path: Path) -> None:
        stubs = tmp_path / "bin"
        stubs.mkdir()
        docker = stubs / "docker"
        docker.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$*" >>"$FAKE_LOG"\n'
            'case "$*" in *" ps -a --format json "*) '
            'printf \'{"ID":"init","State":"exited","ExitCode":0}\\n\';; esac\n',
            encoding="utf-8",
        )
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        log = tmp_path / "docker.log"
        harness = tmp_path / "harness.sh"
        harness.write_text(f'source "{DEPLOY / "watchdog.sh"}"\nrestart_down_services\n', encoding="utf-8")
        run_env = {k: v for k, v in os.environ.items() if not k.startswith("TEATREE_")}
        run_env |= {
            "PATH": f"{stubs}{os.pathsep}{run_env['PATH']}",
            "FAKE_LOG": str(log),
            "TEATREE_GENERATION": "a" * 40,
            "TEATREE_HOST_HOME": "/Users/someone",
            "TEATREE_WATCHDOG_DEPLOY_LOCK": str(tmp_path / "absent.lock"),
        }
        result = subprocess.run([BASH, str(harness)], capture_output=True, text=True, env=run_env, check=False)

        assert result.returncode == 0, result.stderr
        recreation = next(line for line in log.read_text().splitlines() if " up -d --no-recreate " in line)
        assert f"-f {BASE} -f {GENERATION} -f {HOST_IDENTITY}" in recreation
        assert "teatree-worker" in recreation


_T3_DOCKER = (
    '#!/bin/bash\nprintf "image=%s args=%s\\n" "${TEATREE_IMAGE:-}" "$*" >>"$FAKE_LOG"\n'
    'case "$1" in\n'
    'ps) case "$*" in\n'
    '  *" -a "*) [ -n "${STUB_STOPPED:-}" ] && printf "stopped\\n";;\n'
    '  *) [ "$STUB_RUNNING" = 1 ] && printf "worker teatree-worker False\\n";;\n'
    "esac;;\n"
    'image) printf "%s\\n" "$STUB_PROMOTED_SHA";;\n'
    'inspect) printf "%s %s\\n" "${STUB_IMAGE:-teatree-factory:$STUB_SHA}" "$STUB_SHA";;\n'
    'run) case "$*" in\n'
    '  *docker-compose.host-identity.yml*) [ -z "${STUB_FAIL_READ:-}" ] || exit 1; '
    'cat "$STUB_DEPLOY/docker-compose.host-identity.yml";;\n'
    '  *docker-compose.generation.yml*) cat "$STUB_DEPLOY/docker-compose.generation.yml";;\n'
    '  *docker-compose.yml*) cat "$STUB_DEPLOY/docker-compose.yml";;\n'
    "esac;;\n"
    'compose) case "$*" in *"config --images"*) '
    'printf "%s\\n" "${TEATREE_IMAGE:-teatree-headless:latest}";; esac;;\n'
    "esac\n"
)


def _one_off_run(
    tmp_path: Path, *, running: bool, running_sha: str, promoted_sha: str, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    docker = stubs / "docker"
    docker.write_text(_T3_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("TEATREE_")}
    run_env |= {
        "PATH": f"{stubs}{os.pathsep}{run_env['PATH']}",
        "FAKE_LOG": str(log),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "STUB_SHA": running_sha,
        "STUB_PROMOTED_SHA": promoted_sha,
        "STUB_RUNNING": "1" if running else "0",
        "STUB_DEPLOY": str(DEPLOY),
        "TEATREE_HOST_HOME": str(tmp_path / "home"),
        "TEATREE_DOCKER_SOCKET_GID": "0",
        # The host's own deploy lock, held while a real deploy runs, would turn the one-off into a 180s wait.
        "TEATREE_DEPLOY_LOCK": str(tmp_path / "absent.lock"),
        "TEATREE_FORCE_ONE_OFF": "1" if running else "",
        **extra_env,
    }
    # cwd off every checkout: from inside one (the suite's own) the wrapper refuses before dispatching.
    return subprocess.run(
        [BASH, str(DEPLOY / "t3"), "--version"],
        capture_output=True,
        text=True,
        env=run_env,
        check=False,
        cwd=tmp_path,
    )


def _one_off(tmp_path: Path, *, running: bool, running_sha: str, promoted_sha: str, **extra_env: str) -> list[str]:
    result = _one_off_run(tmp_path, running=running, running_sha=running_sha, promoted_sha=promoted_sha, **extra_env)
    assert result.returncode == 0, result.stderr
    return (tmp_path / "docker.log").read_text().splitlines()


def _one_off_line(calls: list[str]) -> str:
    return next(line for line in calls if " run --rm --no-deps " in line)


class TestAOneOffRunsTheServingCode:
    def test_a_stopped_second_stack_uses_its_promoted_image_and_project(self, tmp_path: Path) -> None:
        sha = "b" * 40

        one_off = _one_off_line(
            _one_off(
                tmp_path,
                running=False,
                running_sha="",
                promoted_sha=sha,
                TEATREE_COMPOSE_PROJECT="zddproof",
                COMPOSE_PROJECT_NAME="teatree",
            )
        )

        assert "image=teatree-headless:latest" in one_off
        assert "compose -p zddproof " in one_off

    def test_a_stack_stopped_after_a_failed_promotion_runs_the_generation_its_worker_last_ran(
        self, tmp_path: Path
    ) -> None:
        verified, still_tagged = "a" * 40, "b" * 40

        calls = _one_off(tmp_path, running=False, running_sha=verified, promoted_sha=still_tagged, STUB_STOPPED="1")

        assert f"image=teatree-factory:{verified}" in _one_off_line(calls)
        assert any("args=ps -a -n 1 " in line and "project=teatree " in line for line in calls)

    @pytest.mark.parametrize(
        ("running", "image"),
        [(False, "teatree-headless:latest"), (True, f"teatree-factory:{'a' * 40}")],
        ids=["stopped-service", "forced-one-off"],
    )
    def test_it_uses_the_serving_generation_image_and_topology(
        self, tmp_path: Path, *, running: bool, image: str
    ) -> None:
        sha = "a" * 40
        promoted = "b" * 40 if running else sha

        one_off = _one_off_line(_one_off(tmp_path, running=running, running_sha=sha, promoted_sha=promoted))

        assert f"image={image}" in one_off
        assert f"--project-directory {DEPLOY}" in one_off
        assert "docker-compose.generation.yml" in one_off
        assert "docker-compose.host-identity.yml" in one_off

    def test_a_legacy_image_keeps_the_source_mounted_topology(self, tmp_path: Path) -> None:
        one_off = _one_off_line(_one_off(tmp_path, running=False, running_sha="", promoted_sha=""))

        assert "docker-compose.generation.yml" not in one_off
        assert f"-f {BASE}" in one_off

    def test_the_serving_topology_is_read_from_the_image_once_per_revision(self, tmp_path: Path) -> None:
        sha = "a" * 40
        _one_off(tmp_path, running=False, running_sha=sha, promoted_sha=sha)

        again = _one_off(tmp_path, running=False, running_sha=sha, promoted_sha=sha)

        assert [call for call in again if " args=run " in call] == []
        assert "docker-compose.generation.yml" in _one_off_line(again)

    def test_a_container_created_from_a_registry_repository_runs_that_image(self, tmp_path: Path) -> None:
        sha = "a" * 40
        image = f"registry.example/team/teatree-factory:{sha}"

        calls = _one_off(tmp_path, running=False, running_sha=sha, promoted_sha=sha, STUB_STOPPED="1", STUB_IMAGE=image)

        assert f"image={image}" in _one_off_line(calls)

    def test_reading_the_serving_topology_never_pulls(self, tmp_path: Path) -> None:
        sha = "a" * 40

        calls = _one_off(tmp_path, running=False, running_sha=sha, promoted_sha=sha)

        reads = [call for call in calls if " args=run " in call]
        assert reads
        assert all(" args=run --rm --pull never " in call for call in reads)

    def test_a_failed_topology_read_stops_the_one_off_by_name_and_leaves_nothing_half_written(
        self, tmp_path: Path
    ) -> None:
        sha = "a" * 40

        result = _one_off_run(tmp_path, running=False, running_sha=sha, promoted_sha=sha, STUB_FAIL_READ="1")

        assert result.returncode != 0
        assert "cannot read deploy/docker-compose.host-identity.yml from teatree-headless:latest" in result.stderr
        assert not any(" run --rm --no-deps " in line for line in (tmp_path / "docker.log").read_text().splitlines())
        cached = tmp_path / "cache" / "teatree" / "generation-topology" / sha
        assert sorted(path.name for path in cached.iterdir()) == ["docker-compose.generation.yml", "docker-compose.yml"]


def _admin_argv(tmp_path: Path, **env: str) -> str:
    lines = (DEPLOY / "entrypoint.sh").read_text(encoding="utf-8").splitlines()
    start = lines.index("admin)")
    end = next(index for index in range(start, len(lines)) if lines[index].strip() == ";;")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    t3 = stubs / "t3"
    t3.write_text('#!/bin/bash\nprintf "%s" "$*" >"$FAKE_LOG"\n', encoding="utf-8")
    t3.chmod(t3.stat().st_mode | stat.S_IXUSR)
    harness = tmp_path / "harness.sh"
    arm = "\n".join(lines[start + 1 : end])
    harness.write_text(f"refuse_on_constraint_skew() {{ :; }}\n{arm}\n", encoding="utf-8")
    log = tmp_path / "t3.log"
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("TEATREE_")}
    run_env |= {"PATH": f"{stubs}{os.pathsep}{run_env['PATH']}", "FAKE_LOG": str(log), **env}
    subprocess.run([BASH, str(harness)], capture_output=True, text=True, env=run_env, check=False)
    return log.read_text(encoding="utf-8")


class TestTheAdminBindsItsStacksPort:
    def test_the_live_stack_keeps_port_8000(self, tmp_path: Path) -> None:
        assert _admin_argv(tmp_path) == "admin --host 127.0.0.1 --port 8000 --no-browser"

    def test_a_second_stack_binds_the_port_it_was_given(self, tmp_path: Path) -> None:
        assert _admin_argv(tmp_path, TEATREE_ADMIN_PORT="8100") == "admin --host 127.0.0.1 --port 8100 --no-browser"
