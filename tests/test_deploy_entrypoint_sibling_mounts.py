# test-path: cross-cutting — checks entrypoint sourcing against compose mounts.
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import TypedDict

import pytest


class RenderedVolume(TypedDict):
    source: str
    target: str
    type: str


class RenderedService(TypedDict):
    entrypoint: list[str]
    volumes: list[RenderedVolume]


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
ENTRYPOINT = DEPLOY / "entrypoint.sh"
BASH = shutil.which("bash") or "/bin/bash"
SOURCED = set(re.findall(r'(?m)^\s*(?:\.|source)\s+["\']?\$SCRIPT_DIR/([\w.-]+)', ENTRYPOINT.read_text()))


@pytest.fixture(params=("default", "all-profiles"), scope="module")
def rendered_services(request: pytest.FixtureRequest) -> dict[str, RenderedService]:
    docker = shutil.which("docker")
    assert docker is not None, "Docker Compose is required to check mounted entrypoint files"
    env = {key: value for key, value in os.environ.items() if not key.startswith("TEATREE_")}
    argv = [docker, "compose"]
    if request.param == "all-profiles":
        argv.extend(("--profile", "*"))
    argv.extend(("-f", str(DEPLOY / "docker-compose.yml"), "config", "--format", "json"))
    run = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=30, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)["services"]


def _entrypoint_services(services: dict[str, RenderedService]) -> dict[str, RenderedService]:
    return {
        name: service
        for name, service in services.items()
        if service.get("entrypoint") and service["entrypoint"][-1].endswith("/deploy/entrypoint.sh")
    }


def _mounts(service: RenderedService) -> dict[PurePosixPath, PurePosixPath]:
    return {
        PurePosixPath(volume["target"]): PurePosixPath(volume["source"])
        for volume in service["volumes"]
        if volume["type"] == "bind"
    }


def _mounted(mounts: dict[PurePosixPath, PurePosixPath], path: PurePosixPath) -> bool:
    return any(source == target and (target == path or target in path.parents) for target, source in mounts.items())


def test_entrypoint_sourced_siblings_are_mounted(rendered_services: dict[str, RenderedService]) -> None:
    assert "git-pins.sh" in SOURCED
    services = _entrypoint_services(rendered_services)
    assert {"teatree-webhooks", "teatree-worker", "teatree-watchdog"} <= services.keys()

    for name, service in services.items():
        entrypoint = PurePosixPath(service["entrypoint"][-1])
        mounts = _mounts(service)
        assert _mounted(mounts, entrypoint), name
        for sibling in SOURCED:
            assert _mounted(mounts, entrypoint.parent / sibling), (name, sibling)


def test_entrypoint_reaches_role_dispatch_from_its_mounted_files(
    rendered_services: dict[str, RenderedService], tmp_path: Path
) -> None:
    for name, service in _entrypoint_services(rendered_services).items():
        entrypoint = PurePosixPath(service["entrypoint"][-1])
        mounts = _mounts(service)
        mounted_dir = tmp_path / name / "deploy"
        mounted_dir.mkdir(parents=True)
        shutil.copyfile(ENTRYPOINT, mounted_dir / "entrypoint.sh")
        for sibling in SOURCED:
            if _mounted(mounts, entrypoint.parent / sibling):
                shutil.copyfile(DEPLOY / sibling, mounted_dir / sibling)

        env = {key: value for key, value in os.environ.items() if not key.startswith("TEATREE_")}
        env.update(TEATREE_ROLE="unrecognized", TEATREE_BAKED_ENTRYPOINT=str(tmp_path / "missing"))
        run = subprocess.run(
            [BASH, str(mounted_dir / "entrypoint.sh")], env=env, text=True, capture_output=True, timeout=20, check=False
        )

        assert run.returncode == 64, (name, run.stderr)
        assert "unknown TEATREE_ROLE" in run.stderr
        assert all(sibling not in run.stderr for sibling in SOURCED)
