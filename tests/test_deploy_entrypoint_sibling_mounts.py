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
    environment: dict[str, str]
    volumes: list[RenderedVolume]


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
ENTRYPOINT = DEPLOY / "entrypoint.sh"
BASH = shutil.which("bash") or "/bin/bash"
SCRIPT_LOCATION = r'(?:\$\{?SCRIPT_DIR\}?|\$\(\s*dirname\s+"?\$\{?(?:BASH_SOURCE\[0\]|0)\}?"?\s*\))'
SIBLING_REFERENCE = re.compile(rf"{SCRIPT_LOCATION}/([\w.-]+)")
LOCATION_USE = re.compile(r'SCRIPT_DIR|BASH_SOURCE|dirname\s+"?\$\{?0\b|\$\{0[%#]')
LOCATION_ASSIGNMENT = re.compile(r"^\s*SCRIPT_DIR=")


def sibling_references(script: str) -> set[str]:
    return set(SIBLING_REFERENCE.findall(script))


def unscanned_location_uses(script: str) -> list[str]:
    return [
        line
        for line in script.splitlines()
        if LOCATION_USE.search(line) and not LOCATION_ASSIGNMENT.match(line) and not SIBLING_REFERENCE.search(line)
    ]


SIBLINGS = sibling_references(ENTRYPOINT.read_text())


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


@pytest.mark.parametrize(
    ("script", "siblings"),
    [
        ('. "$SCRIPT_DIR/a.sh"', {"a.sh"}),
        ('    source "${SCRIPT_DIR}/b.sh"', {"b.sh"}),
        ('bash "$SCRIPT_DIR/c.sh" --flag', {"c.sh"}),
        ('[ "$ROLE" = init ] && . "${SCRIPT_DIR}/d.sh"', {"d.sh"}),
        ('x="$(bash "$(dirname "${BASH_SOURCE[0]}")/e.sh" arg)"', {"e.sh"}),
        ('cat "$(dirname "$0")/f.txt"', {"f.txt"}),
        ('. "$SCRIPT_DIR/g.sh"; . "$SCRIPT_DIR/h.sh"', {"g.sh", "h.sh"}),
    ],
)
def test_sibling_references_cover_every_way_a_script_reads_a_neighbour(script: str, siblings: set[str]) -> None:
    assert sibling_references(script) == siblings


@pytest.mark.parametrize(
    "script",
    [
        'cp "$SCRIPT_DIR"/*.sh /tmp',
        'cd "$SCRIPT_DIR" && . ./a.sh',
        'bash "${BASH_SOURCE%/*}/a.sh"',
        'cp "$(dirname "$0")"/*.sh /tmp',
        '. "${0%/*}/a.sh"',
    ],
)
def test_a_location_use_the_scanner_cannot_resolve_is_reported(script: str) -> None:
    assert unscanned_location_uses(script)


def test_the_assignment_and_resolved_references_are_not_reported() -> None:
    script = (
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"\n'
        '. "$SCRIPT_DIR/a.sh"\n'
        'if [ "$(readlink -f "$0")" != "$BAKED" ]; then exit; fi\n'
    )

    assert unscanned_location_uses(script) == []


def test_the_entrypoint_leaves_no_sibling_read_the_scanner_cannot_see() -> None:
    assert {"git-pins.sh", "locked-version.sh"} <= SIBLINGS
    assert unscanned_location_uses(ENTRYPOINT.read_text()) == []


def test_entrypoint_siblings_are_mounted(rendered_services: dict[str, RenderedService]) -> None:
    services = _entrypoint_services(rendered_services)
    assert {"teatree-webhooks", "teatree-worker", "teatree-watchdog"} <= services.keys()

    for name, service in services.items():
        entrypoint = PurePosixPath(service["entrypoint"][-1])
        mounts = _mounts(service)
        assert _mounted(mounts, entrypoint), name
        for sibling in SIBLINGS:
            assert _mounted(mounts, entrypoint.parent / sibling), (name, sibling)


def _stage_mounted_files(service: RenderedService, tmp_path: Path) -> Path:
    entrypoint = PurePosixPath(service["entrypoint"][-1])
    mounts = _mounts(service)
    staged = tmp_path / "deploy"
    staged.mkdir()
    for name in ("entrypoint.sh", *SIBLINGS):
        if _mounted(mounts, entrypoint.parent / name):
            shutil.copyfile(DEPLOY / name, staged / name)
    return staged


def _run_staged_entrypoint(staged: Path, tmp_path: Path, role: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("t3", "exit 0"), ("python", 'printf "%s\\n" "$0 $*"'), ("pass", "printf '%s\\n' canary")):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("TEATREE_")} | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TEATREE_ROLE": role,
        "TEATREE_BAKED_ENTRYPOINT": str(tmp_path / "missing-entrypoint"),
        "TEATREE_HOST_GNUPG_DIR": str(tmp_path / "missing-gnupg"),
        "TEATREE_GNUPG_RUNTIME_DIR": str(tmp_path / "runtime-gnupg"),
        "TEATREE_DISK_TMPDIR": str(tmp_path),
    }
    return subprocess.run(
        [BASH, str(staged / "entrypoint.sh")], env=env, text=True, capture_output=True, timeout=20, check=False
    )


def test_entrypoint_reaches_role_dispatch_from_its_mounted_files(
    rendered_services: dict[str, RenderedService], tmp_path: Path
) -> None:
    for name, service in _entrypoint_services(rendered_services).items():
        service_dir = tmp_path / name
        service_dir.mkdir()
        run = _run_staged_entrypoint(_stage_mounted_files(service, service_dir), service_dir, "unrecognized")

        assert run.returncode == 64, (name, run.stderr)
        assert "unknown TEATREE_ROLE" in run.stderr
        assert all(sibling not in run.stderr for sibling in SIBLINGS)


def test_webhook_listener_starts_from_only_the_files_its_service_mounts(
    rendered_services: dict[str, RenderedService], tmp_path: Path
) -> None:
    service = rendered_services["teatree-webhooks"]
    assert service["environment"]["TEATREE_ROLE"] == "webhook-listener"

    run = _run_staged_entrypoint(_stage_mounted_files(service, tmp_path), tmp_path, "webhook-listener")

    assert run.returncode == 0, run.stderr
    assert run.stdout.rstrip().endswith("teatree.webhook_wsgi:application")
    assert "No such file" not in run.stderr
