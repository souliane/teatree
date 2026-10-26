# test-path: cross-cutting — checks entrypoint sourcing against compose mounts.
import json
import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import TypedDict

import pytest
import yaml

from tests.test_deploy_webhook_listener import _entrypoint


class RenderedVolume(TypedDict):
    source: str
    target: str
    type: str


class RenderedService(TypedDict):
    entrypoint: list[str]
    environment: dict[str, str]
    volumes: list[RenderedVolume | str]


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
ENTRYPOINT = DEPLOY / "entrypoint.sh"
BOX_ENV_FILE = "teatree.env"  # gitignored and box-written: staged empty, never the box's real secrets
SCRIPT_LOCATION = r'(?:\$\{?SCRIPT_DIR\}?|\$\(\s*dirname\s+"?\$\{?(?:BASH_SOURCE\[0\]|0)\}?"?\s*\))'
SIBLING_REFERENCE = re.compile(rf"{SCRIPT_LOCATION}/([\w.-]+)")
LOCATION_USE = re.compile(r"SCRIPT_DIR|BASH_SOURCE|dirname[^\n]*\$0|\$\{0[%#]")
LOCATION_ASSIGNMENT = re.compile(r"^\s*SCRIPT_DIR=")


def sibling_references(script: str) -> set[str]:
    return {
        name
        for line in script.splitlines()
        if not line.lstrip().startswith("#")
        for name in SIBLING_REFERENCE.findall(line)
    }


def sibling_closure(script: Path) -> set[str]:
    siblings: set[str] = set()
    pending = [script]
    while pending:
        current = pending.pop()
        for name in sibling_references(current.read_text(encoding="utf-8")) - siblings:
            siblings.add(name)
            neighbour = current.parent / name
            if neighbour.suffix == ".sh" and neighbour.exists():
                pending.append(neighbour)
    return siblings


def unscanned_location_uses(script: str) -> list[str]:
    return [
        line
        for line in script.splitlines()
        if not line.lstrip().startswith("#")
        and not LOCATION_ASSIGNMENT.match(line)
        and LOCATION_USE.search(SIBLING_REFERENCE.sub("", line))
    ]


ENTRYPOINT_TEXT = ENTRYPOINT.read_text(encoding="utf-8")
SIBLINGS = sibling_closure(ENTRYPOINT)
PRE_DISPATCH_SIBLINGS = sibling_references(ENTRYPOINT_TEXT.split('if [ "$ROLE" = webhook-listener ]', 1)[0])
for name in tuple(PRE_DISPATCH_SIBLINGS):
    PRE_DISPATCH_SIBLINGS.update(sibling_closure(DEPLOY / name))


@pytest.fixture(scope="module")
def rendered_services() -> dict[str, RenderedService]:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


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
        if isinstance(volume, dict) and volume["type"] == "bind"
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


def test_a_readlink_based_location_use_is_reported() -> None:
    script = '. "$(dirname "$(readlink -f "$0")")/missing.sh"'

    assert unscanned_location_uses(script) == [script]


def test_a_resolved_reference_does_not_hide_an_unresolved_one_on_the_same_line() -> None:
    script = '. "$SCRIPT_DIR/a.sh"; cp "$SCRIPT_DIR"/*.sh /tmp'

    assert unscanned_location_uses(script) == [script]


def test_comments_do_not_create_sibling_requirements() -> None:
    assert sibling_references('# . "$SCRIPT_DIR/imaginary.sh"\n. "$SCRIPT_DIR/real.sh"') == {"real.sh"}


def test_sibling_reads_are_followed_through_sourced_files(tmp_path: Path) -> None:
    entrypoint = tmp_path / "entrypoint.sh"
    entrypoint.write_text('. "$SCRIPT_DIR/first.sh"\n', encoding="utf-8")
    (tmp_path / "first.sh").write_text('. "$SCRIPT_DIR/second.sh"\n', encoding="utf-8")
    (tmp_path / "second.sh").write_text("true\n", encoding="utf-8")

    assert sibling_closure(entrypoint) == {"first.sh", "second.sh"}


def test_the_assignment_and_resolved_references_are_not_reported() -> None:
    script = (
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"\n'
        '. "$SCRIPT_DIR/a.sh"\n'
        'if [ "$(readlink -f "$0")" != "$BAKED" ]; then exit; fi\n'
    )

    assert unscanned_location_uses(script) == []


def test_the_entrypoint_leaves_no_sibling_read_the_scanner_cannot_see() -> None:
    assert {"git-pins.sh", "locked-version.sh"} <= SIBLINGS
    for script in (ENTRYPOINT, *(DEPLOY / name for name in SIBLINGS if name.endswith(".sh"))):
        assert unscanned_location_uses(script.read_text(encoding="utf-8")) == [], script
    assert {"git-pins.sh"} <= PRE_DISPATCH_SIBLINGS


def test_entrypoint_siblings_are_mounted(rendered_services: dict[str, RenderedService]) -> None:
    services = _entrypoint_services(rendered_services)
    assert {"teatree-webhooks", "teatree-worker", "teatree-watchdog"} <= services.keys()

    for name, service in services.items():
        entrypoint = PurePosixPath(service["entrypoint"][-1])
        mounts = _mounts(service)
        assert _mounted(mounts, entrypoint), name
        required = PRE_DISPATCH_SIBLINGS if name == "teatree-webhooks" else SIBLINGS
        for sibling in required:
            assert _mounted(mounts, entrypoint.parent / sibling), (name, sibling)

    listener = services["teatree-webhooks"]
    assert not _mounted(_mounts(listener), PurePosixPath(listener["entrypoint"][-1]).parent / "locked-version.sh")


@pytest.mark.parametrize("checkout", [None, "/srv/custom-checkout"])
def test_compose_render_mounts_entrypoint_siblings_for_every_profile(
    rendered_services: dict[str, RenderedService], tmp_path: Path, checkout: str | None
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is unavailable")

    (tmp_path / "docker-compose.yml").write_text((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    (tmp_path / "teatree.env").write_text("T3_DEBUG=0\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key not in {"COMPOSE_FILE", "COMPOSE_PROFILES"}}
    if checkout is None:
        env.pop("TEATREE_DEPLOY_CHECKOUT", None)
    else:
        env["TEATREE_DEPLOY_CHECKOUT"] = checkout
    env.setdefault("DOCKER_CONFIG", str(Path(pwd.getpwuid(os.getuid()).pw_dir) / ".docker"))
    result = subprocess.run(
        [docker, "compose", "--profile", "*", "-f", str(tmp_path / "docker-compose.yml"), "config", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    services = _entrypoint_services(json.loads(result.stdout)["services"])
    assert services.keys() == _entrypoint_services(rendered_services).keys()
    for name, service in services.items():
        entrypoint = PurePosixPath(service["entrypoint"][-1])
        mounts = _mounts(service)
        required = PRE_DISPATCH_SIBLINGS if name == "teatree-webhooks" else SIBLINGS
        for sibling in required:
            assert _mounted(mounts, entrypoint.parent / sibling), (name, sibling)


def _stage_mounted_files(service: RenderedService, tmp_path: Path) -> Path:
    entrypoint = PurePosixPath(service["entrypoint"][-1])
    mounts = _mounts(service)
    staged = tmp_path / "deploy"
    staged.mkdir()
    for name in ("entrypoint.sh", *SIBLINGS):
        if not _mounted(mounts, entrypoint.parent / name):
            continue
        if name == BOX_ENV_FILE:
            (staged / name).touch()
        else:
            shutil.copy(DEPLOY / name, staged / name)
    return staged


def _run_staged_entrypoint(staged: Path, tmp_path: Path, role: str) -> subprocess.CompletedProcess[str]:
    return _entrypoint(tmp_path, role, script=staged / "entrypoint.sh")


def test_entrypoint_reaches_role_dispatch_from_its_mounted_files(
    rendered_services: dict[str, RenderedService], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller_config = tmp_path / "caller.gitconfig"
    caller_config.write_text("[user]\n\tname = Real Dev\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(caller_config))

    for name, service in _entrypoint_services(rendered_services).items():
        if name == "teatree-webhooks":  # execs the listener before secret sourcing: its own test drives its real role
            continue
        service_dir = tmp_path / name
        service_dir.mkdir()
        run = _run_staged_entrypoint(_stage_mounted_files(service, service_dir), service_dir, "unrecognized")

        assert run.returncode == 64, (name, run.stderr)
        assert "unknown TEATREE_ROLE" in run.stderr
        assert all(sibling not in run.stderr for sibling in SIBLINGS)

    assert caller_config.read_text(encoding="utf-8") == "[user]\n\tname = Real Dev\n"


def test_webhook_listener_starts_from_only_the_files_its_service_mounts(
    rendered_services: dict[str, RenderedService], tmp_path: Path
) -> None:
    service = rendered_services["teatree-webhooks"]
    assert service["environment"]["TEATREE_ROLE"] == "webhook-listener"

    run = _run_staged_entrypoint(_stage_mounted_files(service, tmp_path), tmp_path, "webhook-listener")

    assert run.returncode == 0, run.stderr
    assert run.stdout.rstrip().endswith("teatree.webhook_wsgi:application")
    assert "No such file" not in run.stderr
