"""The headless stack externalizes its state onto host bind mounts.

PR-1 of the Docker unification: the factory's DB, worktrees, and workspaces
must live on the host at their canonical absolute paths (not in Docker named
volumes), so the container and the host converge on ONE db.sqlite3 via path
identity (`deploy/Dockerfile` sets no `XDG_DATA_HOME`, HOME is `/home/teatree`
in both). The credential plane (the host pass store + its GPG home) is a further
dedicated bind mount, decoupled from the data dir so a data-dir change can never
orphan the provisioned credential store again (the #3262 regression). These tests
pin the compose file to bind mounts at those exact host paths and confirm the
now-unused named-volume declarations are gone.

Structure is parsed from the YAML directly (the source of truth); a golden
`docker compose config` assertion runs too when a usable docker is present.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

COMPOSE_FILE = Path(__file__).resolve().parents[1] / "deploy" / "docker-compose.yml"

# The three externalized state mounts: canonical host path == container path.
EXTERNALIZED = {
    "/home/teatree/.local/share/teatree",
    "/home/teatree/.local/share/teatree-worktrees",
    "/home/teatree/workspace/t3-workspaces",
}
# The credential plane: the host pass store + its GPG home, DEDICATED bind mounts
# decoupled from the data dir so a data-dir change can never orphan the
# provisioned credential store again (#3262 regression). Also path identity.
CREDENTIAL_PLANE = {
    "/home/teatree/.password-store",
    "/home/teatree/.gnupg",
}
# Every host bind mount the shared list must carry, by canonical source path.
ALL_BIND_SOURCES = EXTERNALIZED | CREDENTIAL_PLANE
# The two mounts that stay Docker-managed named volumes (later PRs handle these).
KEPT_NAMED_VOLUMES = {"teatree_src", "teatree_uv"}
REMOVED_NAMED_VOLUMES = {"teatree_data", "teatree_worktrees", "teatree_workspaces"}


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _common_volumes() -> list:
    """The shared mount list every service inherits via `*teatree-common`."""
    return _compose()["x-teatree-common"]["volumes"]


class TestExternalizedBindMounts:
    def _bind_mounts(self) -> dict:
        return {
            entry["source"]: entry
            for entry in _common_volumes()
            if isinstance(entry, dict) and entry.get("type") == "bind"
        }

    def test_state_dirs_are_bind_mounts_at_canonical_host_paths(self) -> None:
        binds = self._bind_mounts()
        assert set(binds) == ALL_BIND_SOURCES, "every state + credential dir must be a host bind mount"
        for source, entry in binds.items():
            # Path identity: the host source and the container target are the same
            # absolute path, so both see the same files with no XDG knob.
            assert entry["target"] == source, f"{source}: bind target must equal host source"

    def test_credential_plane_is_a_dedicated_bind_mount(self) -> None:
        # The pass store + GPG home must be their own mounts (not nested under the
        # data dir), so externalizing/moving the data dir never orphans them again.
        binds = self._bind_mounts()
        assert CREDENTIAL_PLANE <= set(binds), "pass store + GPG home must be host bind mounts"
        for source in CREDENTIAL_PLANE:
            assert not source.startswith("/home/teatree/.local/share/teatree"), (
                f"{source}: credential plane must be decoupled from the data dir"
            )

    def test_kept_named_volume_mounts_still_present(self) -> None:
        named = {entry.split(":", 1)[0] for entry in _common_volumes() if isinstance(entry, str)}
        assert named == KEPT_NAMED_VOLUMES

    def test_no_state_dir_uses_a_named_volume_mount(self) -> None:
        named = {entry.split(":", 1)[0] for entry in _common_volumes() if isinstance(entry, str)}
        assert named.isdisjoint(REMOVED_NAMED_VOLUMES), "state dirs must not mount named volumes"


class TestTopLevelVolumeDeclarations:
    def test_unused_named_volume_declarations_removed(self) -> None:
        declared = set(_compose().get("volumes") or {})
        assert declared == KEPT_NAMED_VOLUMES
        assert declared.isdisjoint(REMOVED_NAMED_VOLUMES)


class TestDockerComposeConfigGolden:
    """Golden: `docker compose config` resolves the same bind mounts end to end."""

    def test_rendered_config_carries_the_bind_mounts(self, tmp_path: Path) -> None:
        docker = shutil.which("docker")
        if docker is None:
            pytest.skip("docker not available for the golden config render")
        # Render from an isolated copy with a stub env_file so we never touch the
        # real box secrets; `config` neither builds nor starts anything.
        work = tmp_path / "deploy"
        work.mkdir()
        (work / "docker-compose.yml").write_text(COMPOSE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        (work / "teatree.env").write_text("T3_DEBUG=0\n", encoding="utf-8")
        proc = subprocess.run(
            [docker, "compose", "-f", str(work / "docker-compose.yml"), "config", "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            pytest.skip(f"docker compose config unusable here: {proc.stderr.strip()[:200]}")
        rendered = json.loads(proc.stdout)
        mounts = {m["source"]: m for svc in rendered["services"].values() for m in svc.get("volumes", [])}
        for path in ALL_BIND_SOURCES:
            assert path in mounts, f"{path} missing from rendered config"
            assert mounts[path]["type"] == "bind"
            assert mounts[path]["target"] == path


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
