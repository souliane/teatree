"""The deploy invariants that keep the control DB off the host filesystem.

Three things must hold together or the isolation is theatre: every service that runs
`t3` mounts the control-DB volume at the path `teatree.paths` resolves to, the data dir
stays a HOST bind mount (backups, the handover mirror and the host projection all live
there), and the image pre-creates the mount point so a fresh volume inherits the runtime
user's ownership rather than root's.
"""

from pathlib import Path

import yaml

from teatree.paths import DEFAULT_CONTROL_DB_DIR

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_COMPOSE = _DEPLOY / "docker-compose.yml"
_DOCKERFILE = _DEPLOY / "Dockerfile"

_CONTROL_DB_VOLUME = "teatree_control_db"
_DATA_DIR_TARGET = "/home/teatree/.local/share/teatree"

#: The watchdog drives the docker socket and never opens the control DB, so it is
#: deliberately not on the anchor and deliberately not asserted here.
_T3_SERVICES = ("teatree-init", "teatree-worker", "teatree-admin", "teatree-slack-listener")


def _compose() -> dict:
    # SafeLoader resolves the `&teatree-common` anchor and `<<` merge keys, so each
    # service's effective mount list is what the role actually runs with.
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))


def _mounts(compose: dict, service: str) -> list[object]:
    return compose["services"][service].get("volumes", [])


class TestControlDbHasItsOwnVolume:
    def test_every_t3_service_mounts_it_at_the_resolved_path(self) -> None:
        compose = _compose()
        expected = f"{_CONTROL_DB_VOLUME}:{DEFAULT_CONTROL_DB_DIR}"

        for service in _T3_SERVICES:
            assert expected in _mounts(compose, service), (
                f"{service} must mount the control-DB volume at {DEFAULT_CONTROL_DB_DIR}; "
                f"a service without it resolves a directory that does not exist and cannot open the DB"
            )

    def test_it_is_declared_as_a_named_volume(self) -> None:
        assert _CONTROL_DB_VOLUME in _compose()["volumes"]

    def test_it_is_mounted_as_a_directory_never_a_file(self) -> None:
        # The `-wal`/`-shm` sidecars must land beside the database. Mounting the file
        # would strand them on the container's own disk layer.
        compose = _compose()
        for service in _T3_SERVICES:
            for mount in _mounts(compose, service):
                if isinstance(mount, str) and mount.startswith(f"{_CONTROL_DB_VOLUME}:"):
                    assert not mount.split(":", 1)[1].endswith(".sqlite3")

    def test_every_t3_service_is_told_where_it_is(self) -> None:
        compose = _compose()
        for service in _T3_SERVICES:
            environment = compose["services"][service]["environment"]
            assert environment["T3_CONTROL_DB_DIR"] == str(DEFAULT_CONTROL_DB_DIR)


class TestDataDirStaysAHostBindMount:
    def test_the_data_dir_is_a_bind_not_a_volume(self) -> None:
        compose = _compose()
        binds = [
            mount
            for mount in _mounts(compose, "teatree-worker")
            if isinstance(mount, dict) and mount.get("target") == _DATA_DIR_TARGET
        ]

        assert [mount["type"] for mount in binds] == ["bind"], (
            "backups/, the handover mirror and the host projection must stay host-visible"
        )

    def test_no_service_mounts_a_volume_over_the_data_dir(self) -> None:
        compose = _compose()
        for service in _T3_SERVICES:
            for mount in _mounts(compose, service):
                if isinstance(mount, str):
                    assert not mount.endswith(f":{_DATA_DIR_TARGET}")

    def test_the_control_db_is_not_inside_the_data_dir(self) -> None:
        assert _DATA_DIR_TARGET not in str(DEFAULT_CONTROL_DB_DIR)


class TestImagePreCreatesTheMountPoint:
    def test_the_control_db_dir_exists_in_the_image(self) -> None:
        # Docker seeds a fresh named volume from the image directory, ownership
        # included; without this the volume arrives root-owned and the non-root
        # runtime user cannot create the database in it.
        assert str(DEFAULT_CONTROL_DB_DIR) in _DOCKERFILE.read_text(encoding="utf-8")

    def test_it_is_owned_by_the_runtime_user_after_the_uid_renumber(self) -> None:
        chowns = [
            line for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines() if "chown -R teatree:teatree" in line
        ]

        assert chowns, "the image must chown its mount points"
        for line in chowns:
            assert "/var/lib/teatree" in line, (
                "both chown passes must cover the control-DB mount point — the second runs "
                f"after the UID renumber, so missing it leaves the volume unwritable: {line}"
            )


class TestMigrationScriptIsShipped:
    def test_the_online_backup_migration_exists_and_is_executable(self) -> None:
        script = _DEPLOY / "migrate-control-db-to-volume.sh"

        assert script.is_file()
        assert script.stat().st_mode & 0o111

    def test_it_verifies_integrity_foreign_keys_and_row_counts(self) -> None:
        body = (_DEPLOY / "migrate-control-db-to-volume.sh").read_text(encoding="utf-8")

        assert ".backup" in body, "a file copy of a live SQLite database is how the broken backups happened"
        assert "PRAGMA integrity_check" in body
        assert "PRAGMA foreign_key_check" in body
        assert "row_counts" in body
