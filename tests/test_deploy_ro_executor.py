# test-path: cross-cutting — pins deploy/ against the executor contract in teatree.agents.
"""The read-only executor's compose block and image stage are the contract its live probe enforces."""

import hashlib
import json
import re
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

from teatree.agents.codex_read_only_executor import EXECUTOR_SERVICE
from teatree.agents.codex_read_only_verdict import (
    CHECKOUT_ENV,
    CLONE_ROOT,
    DECLARED_TMPFS,
    ENV_ALLOWLIST,
    FORBIDDEN_TOOLS,
    WORKTREE_ROOT,
)

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_DOCKERFILE = _DEPLOY / "Dockerfile"
_SERVICE = EXECUTOR_SERVICE
_NOBODY = "65534:65534"
_WORKER_DIGEST_AT_BASE = "b10b4113dadb7b3d3c01daaca1e22ae00adcb25c16afbfa2aff78424d685d548"


class _Volume(TypedDict):
    type: str
    source: str
    target: str
    read_only: bool


class _Build(TypedDict):
    target: str


class _Service(TypedDict, total=False):
    profiles: list[str]
    image: str
    build: _Build
    user: str
    read_only: bool
    cap_drop: list[str]
    security_opt: list[str]
    environment: dict[str, str]
    volumes: list[_Volume]
    tmpfs: list[str]
    networks: list[str]


class _Worker(TypedDict):
    environment: dict[str, str]
    volumes: list[_Volume | str]


class _Compose(TypedDict):
    services: dict[str, _Service]
    networks: dict[str, None]


def _compose() -> _Compose:
    compose: _Compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    return compose


def _executor() -> _Service:
    return _compose()["services"][_SERVICE]


def _worker() -> _Worker:
    worker: _Worker = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]["teatree-worker"]
    return worker


def _worker_mount_source(target: str) -> str:
    return next(
        mount["source"] for mount in _worker()["volumes"] if not isinstance(mount, str) and mount["target"] == target
    )


_CHECKOUT = _worker()["environment"][CHECKOUT_ENV]
_CHECKOUT_GIT = f"{_CHECKOUT}/.git"


def _stages() -> dict[str, list[str]]:
    stages: dict[str, list[str]] = {}
    current: list[str] = []
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if match := re.match(r"FROM\s+(\S+)\s+AS\s+(\S+)", line):
            current = stages.setdefault(match.group(2), [])
        current.append(line)
    return stages


def test_worker_service_is_unchanged() -> None:
    worker = _worker()

    assert hashlib.sha256(json.dumps(worker, sort_keys=True).encode()).hexdigest() == _WORKER_DIGEST_AT_BASE


def test_no_whole_project_build_or_up_reaches_the_executor() -> None:
    assert _executor()["profiles"] == ["ro-executor"]
    for script in ("deploy.sh", "watchdog.sh", "roll.sh", "t3", "docker-compose.generation.yml"):
        text = (_DEPLOY / script).read_text(encoding="utf-8")
        assert "COMPOSE_PROFILES" not in text, script
        assert "--profile" not in text, script


def test_executor_is_never_on_the_host_network() -> None:
    executor = _executor()

    assert "network_mode" not in executor
    assert executor["networks"] == ["ro-executor"]
    assert _compose()["networks"]["ro-executor"] is None


def test_executor_mounts_and_env_are_exactly_the_declared_set() -> None:
    executor = _executor()
    volumes = {mount["target"]: mount for mount in executor["volumes"]}

    assert set(volumes) == {_CHECKOUT_GIT, CLONE_ROOT, WORKTREE_ROOT}
    assert all(mount["read_only"] is True for mount in volumes.values())
    assert volumes[_CHECKOUT_GIT] == {
        "type": "bind",
        "source": _CHECKOUT_GIT,
        "target": _CHECKOUT_GIT,
        "read_only": True,
    }
    assert volumes[CLONE_ROOT]["source"] == "teatree_clones"
    assert volumes[WORKTREE_ROOT]["source"] == _worker_mount_source(WORKTREE_ROOT)
    assert {entry.split(":", 1)[0] for entry in executor["tmpfs"]} == DECLARED_TMPFS
    assert set(executor["environment"]) == {"TMPDIR", "GIT_OPTIONAL_LOCKS", "LANG", "TZ"}
    assert set(executor["environment"]) <= ENV_ALLOWLIST
    assert executor["read_only"] is True
    assert executor["cap_drop"] == ["ALL"]
    assert executor["security_opt"] == ["no-new-privileges:true"]


def test_executor_never_mounts_the_directory_that_holds_the_box_secrets() -> None:
    sources = [mount["source"] for mount in _executor()["volumes"]]

    assert not [source for source in sources if f"{_CHECKOUT}/deploy/teatree.env".startswith(f"{source}/")]


def test_executor_runs_as_a_uid_that_owns_none_of_the_mounted_trees() -> None:
    executor = _executor()
    uid, gid = _NOBODY.split(":")

    assert executor["user"] == _NOBODY
    assert "TEATREE_UID" not in json.dumps(executor)
    assert all(f"uid={uid},gid={gid}," in entry for entry in executor["tmpfs"])
    assert f"USER {_NOBODY}" in _stages()["ro-executor"]


@pytest.mark.parametrize(
    "key", ["env_file", "group_add", "privileged", "cap_add", "devices", "pid", "ipc", "volumes_from", "secrets"]
)
def test_executor_declares_no_grant(key: str) -> None:
    assert key not in _executor()


@pytest.mark.parametrize("fragment", ["docker.sock", "control-db", "password-store", "gnupg", "/proc", "TOKEN"])
def test_executor_reaches_no_credential_or_control_plane(fragment: str) -> None:
    assert fragment not in json.dumps(_executor())


def test_executor_stage_is_minimal() -> None:
    stages = _stages()
    executor_stage = stages["ro-executor"]
    system_base = stages["system"][0].split()[1]
    instructions = "\n".join(executor_stage)
    installed = re.findall(r"install -y -qq --no-install-recommends ([^&]+)&&", instructions)

    assert executor_stage[0].split()[1] == system_base
    assert not re.search(r"^ENV\s", instructions, re.MULTILINE)
    assert installed == ["ca-certificates git python3 tini "]
    assert "git config --system safe.directory '*'" in instructions
    assert "useradd" not in instructions
    assert not set(installed[0].split()) & {*FORBIDDEN_TOOLS, "docker-ce-cli", "gnupg", "curl", "nodejs"}
    assert "npm install" not in instructions
    assert _executor()["build"]["target"] == "ro-executor"
    assert _executor()["image"] == "teatree-ro-executor:local"
