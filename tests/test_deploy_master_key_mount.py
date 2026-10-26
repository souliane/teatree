# test-path: cross-cutting — pins deploy/docker-compose*.yml and deploy/README.md; no src mirror.
"""The sealed-store master key reaches exactly the services that mount the password store, read-only."""

import re
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
BASE = DEPLOY / "docker-compose.yml"
GENERATION = DEPLOY / "docker-compose.generation.yml"
README = DEPLOY / "README.md"

KEY_TARGET = "/home/teatree/.config/teatree-master-key"  # privacy-scan:allow — documented container home
KEY_SOURCE_VALUE = f"file:{KEY_TARGET}/master.key"
PASSWORD_STORE_TARGET = "/home/teatree/.password-store"  # privacy-scan:allow — documented container home
KEY_SERVICES = {
    "teatree-init",
    "teatree-worker",
    "teatree-admin",
    "teatree-webhooks",
    "teatree-slack-listener",
}


class _ComposeLoader(yaml.SafeLoader):
    pass


_ComposeLoader.add_constructor("!reset", lambda _loader, _node: None)
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


def _mount(entry: str | dict) -> dict:
    if isinstance(entry, dict):
        return {"source": entry.get("source", ""), "target": entry["target"], "read_only": bool(entry.get("read_only"))}
    plain = re.sub(r"\$\{[^}]*\}", "_", entry)
    _, target, *mode = plain.split(":")
    return {"source": entry.split(":" + target)[0], "target": target, "read_only": mode == ["ro"]}


def _mounts_by_service(path: Path) -> dict[str, dict[str, dict]]:
    services = _load(path)["services"]
    return {
        name: {mount["target"]: mount for mount in map(_mount, service.get("volumes") or [])}
        for name, service in services.items()
    }


def _services_mounting(path: Path, target: str) -> set[str]:
    return {name for name, mounts in _mounts_by_service(path).items() if target in mounts}


@pytest.mark.parametrize("compose", [BASE, GENERATION], ids=["base", "generation"])
class TestKeyMount:
    def test_the_key_directory_reaches_exactly_the_password_store_services(self, compose: Path) -> None:
        assert _services_mounting(compose, KEY_TARGET) == _services_mounting(compose, PASSWORD_STORE_TARGET)
        assert _services_mounting(compose, KEY_TARGET) == KEY_SERVICES

    def test_the_mount_is_read_only_and_never_the_control_db_volume(self, compose: Path) -> None:
        for service in KEY_SERVICES:
            mount = _mounts_by_service(compose)[service][KEY_TARGET]
            assert mount["read_only"] is True
            assert "control_db" not in mount["source"]
            assert mount["source"].endswith("/.config/teatree-master-key")

    @pytest.mark.parametrize("service", ["teatree-watchdog", "teatree-roller", "teatree-ro-executor", "ro-executor"])
    def test_unrelated_services_never_mount_the_key(self, compose: Path, service: str) -> None:
        assert KEY_TARGET not in _mounts_by_service(compose).get(service, {})


def test_each_key_service_names_the_mounted_file_as_its_source() -> None:
    services = _load(BASE)["services"]
    assert {name for name, service in services.items() if "T3_MASTER_KEY_SOURCE" in service["environment"]} == (
        KEY_SERVICES
    )
    assert {services[name]["environment"]["T3_MASTER_KEY_SOURCE"] for name in KEY_SERVICES} == {KEY_SOURCE_VALUE}


def test_the_shared_create_env_anchor_does_not_carry_the_key_source() -> None:
    assert "T3_MASTER_KEY_SOURCE" not in _load(BASE)["x-teatree-create-env"]


class TestReadme:
    def test_it_states_where_the_key_lives_and_that_a_volume_backup_must_not_carry_it(self) -> None:
        text = " ".join(README.read_text(encoding="utf-8").split())
        assert "T3_MASTER_KEY_SOURCE" in text
        assert "teatree-master-key" in text
        assert "A whole-volume backup or export of `teatree_control_db` must not carry the key" in text
        assert "keeps its master key in the `teatree_control_db`" not in text
