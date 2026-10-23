# test-path: cross-cutting — checks entrypoint sourcing against compose mounts.
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
ENTRYPOINT = DEPLOY / "entrypoint.sh"
BASH = shutil.which("bash") or "/bin/bash"
SOURCED = set(re.findall(r'(?m)^\s*(?:\.|source)\s+["\']?\$SCRIPT_DIR/([\w.-]+)', ENTRYPOINT.read_text()))
VARIABLE_WITH_DEFAULT = re.compile(r"\$\{\w+:-([^}]*)\}")
SERVICES = yaml.safe_load(VARIABLE_WITH_DEFAULT.sub(r"\1", (DEPLOY / "docker-compose.yml").read_text()))["services"]
ENTRYPOINT_SERVICES = {
    name: service
    for name, service in SERVICES.items()
    if service.get("entrypoint", [""])[-1].endswith("/deploy/entrypoint.sh")
}


def _mounts(service: dict[str, Any]) -> dict[PurePosixPath, PurePosixPath]:
    mounts = {}
    for volume in service["volumes"]:
        if isinstance(volume, dict):
            source, target = volume.get("source", ""), volume["target"]
        else:
            source, target = volume.split(":")[:2]
        if source.startswith("/"):
            mounts[PurePosixPath(target)] = PurePosixPath(source)
    return mounts


def _mounted(mounts: dict[PurePosixPath, PurePosixPath], path: PurePosixPath) -> bool:
    return any(source == target and (target == path or target in path.parents) for target, source in mounts.items())


@pytest.mark.parametrize("name", ENTRYPOINT_SERVICES)
def test_entrypoint_sourced_siblings_are_mounted(name: str) -> None:
    assert "git-pins.sh" in SOURCED
    assert {"teatree-webhooks", "teatree-worker", "teatree-watchdog"} <= ENTRYPOINT_SERVICES.keys()
    service = ENTRYPOINT_SERVICES[name]
    entrypoint = PurePosixPath(service["entrypoint"][-1])
    mounts = _mounts(service)

    assert _mounted(mounts, entrypoint), name
    for sibling in SOURCED:
        assert _mounted(mounts, entrypoint.parent / sibling), (name, sibling)


@pytest.mark.parametrize("name", ENTRYPOINT_SERVICES)
def test_entrypoint_reaches_role_dispatch_from_its_mounted_files(name: str, tmp_path: Path) -> None:
    service = ENTRYPOINT_SERVICES[name]
    entrypoint = PurePosixPath(service["entrypoint"][-1])
    mounts = _mounts(service)
    mounted_dir = tmp_path / "deploy"
    mounted_dir.mkdir()
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
