"""Tests for the one-time named-volume -> host-bind-mount migration script.

`deploy/migrate-volume-data.sh` is the SAFE operator step for PR-1 of the Docker
unification: it archives the existing host DB (timestamped, never deleted),
copies the real factory state out of the Docker named volumes onto the host bind
paths, and brings the stack up. It must REFUSE while the stack is up and must
ARCHIVE before it overwrites.

Following the Test-Writing Doctrine (mirrors `test_deploy_entrypoint_disable_loops`
and `test_refuse_public_push_with_leak`) these run the REAL script in a bash
subprocess with a stub `docker` on PATH and every path env-overridden into
`tmp_path` — nothing about the shell logic is reimplemented.
"""

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "migrate-volume-data.sh"
_BASH = shutil.which("bash") or "bash"


def _write_docker_stub(bin_dir: Path) -> None:
    """A `docker` shim: `compose ps -q` prints STUB_RUNNING_ID; `up` logs the call."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'args="$*"\n'
        'case "$args" in\n'
        '  *"ps -q"*) printf "%s" "${STUB_RUNNING_ID:-}"; exit 0 ;;\n'
        '  *" up "*|*" up") echo "$args" >> "$STUB_UP_LOG"; exit 0 ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class _Env:
    """A prepared tmp filesystem: fake host dirs, fake volume dirs, stub docker."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin = tmp_path / "bin"
        _write_docker_stub(self.bin)

        self.host_data = tmp_path / "host" / "teatree"
        self.host_worktrees = tmp_path / "host" / "teatree-worktrees"
        self.host_workspaces = tmp_path / "host" / "t3-workspaces"
        self.vol_data = tmp_path / "vol" / "data" / "_data"
        self.vol_worktrees = tmp_path / "vol" / "worktrees" / "_data"
        self.vol_workspaces = tmp_path / "vol" / "workspaces" / "_data"
        self.archive_root = tmp_path / "host"
        for d in (
            self.host_data,
            self.host_worktrees,
            self.host_workspaces,
            self.vol_data,
            self.vol_worktrees,
            self.vol_workspaces,
        ):
            d.mkdir(parents=True, exist_ok=True)

        # The host DB the operator has today (about to be archived + overwritten).
        (self.host_data / "db.sqlite3").write_text("HOST_DB_ORIGINAL", encoding="utf-8")
        # The real factory state living in the container volume (must win).
        (self.vol_data / "db.sqlite3").write_text("VOLUME_DB_FACTORY", encoding="utf-8")
        (self.vol_data / "instance_id").write_text("iid-123", encoding="utf-8")
        (self.vol_data / ".password-store").mkdir()
        (self.vol_worktrees / "wt-1").write_text("worktree-state", encoding="utf-8")
        (self.vol_workspaces / "ws-1").write_text("workspace-state", encoding="utf-8")

        self.up_log = tmp_path / "up.log"
        self.up_log.write_text("", encoding="utf-8")
        self.compose = tmp_path / "docker-compose.yml"
        self.compose.write_text("name: teatree\n", encoding="utf-8")

    def run(self, *, running_id: str = "") -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env['PATH']}"
        env["STUB_RUNNING_ID"] = running_id
        env["STUB_UP_LOG"] = str(self.up_log)
        env["MIGRATE_DOCKER"] = "docker"
        env["MIGRATE_COMPOSE_FILE"] = str(self.compose)
        env["MIGRATE_HOST_DATA"] = str(self.host_data)
        env["MIGRATE_HOST_WORKTREES"] = str(self.host_worktrees)
        env["MIGRATE_HOST_WORKSPACES"] = str(self.host_workspaces)
        env["MIGRATE_VOL_DATA"] = str(self.vol_data)
        env["MIGRATE_VOL_WORKTREES"] = str(self.vol_worktrees)
        env["MIGRATE_VOL_WORKSPACES"] = str(self.vol_workspaces)
        env["MIGRATE_ARCHIVE_ROOT"] = str(self.archive_root)
        return subprocess.run(
            [_BASH, str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def archives(self) -> list[Path]:
        return sorted(self.archive_root.glob("teatree-bindmount-archive-*"))


class TestScriptIsClean:
    def test_bash_syntax_is_valid(self) -> None:
        proc = subprocess.run([_BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr

    def test_shellcheck_clean(self) -> None:
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            pytest.skip("shellcheck not installed")
        proc = subprocess.run([shellcheck, str(SCRIPT)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr


class TestRefusesWhenStackUp:
    def test_refuses_and_touches_nothing(self, tmp_path: Path) -> None:
        env = _Env(tmp_path)
        result = env.run(running_id="deadbeefcafe")
        assert result.returncode != 0
        assert "Stop it first" in result.stderr
        # Nothing archived, nothing overwritten, stack not brought up.
        assert env.archives() == []
        assert (env.host_data / "db.sqlite3").read_text(encoding="utf-8") == "HOST_DB_ORIGINAL"
        assert env.up_log.read_text(encoding="utf-8") == ""


class TestArchivesBeforeOverwrite:
    def test_host_db_archived_then_overwritten_from_volume(self, tmp_path: Path) -> None:
        env = _Env(tmp_path)
        result = env.run(running_id="")
        assert result.returncode == 0, result.stderr

        archives = env.archives()
        assert len(archives) == 1, "exactly one timestamped archive per run"
        # The original host DB is preserved in the archive (never deleted)...
        assert (archives[0] / "db.sqlite3").read_text(encoding="utf-8") == "HOST_DB_ORIGINAL"
        # ...and the host DB is now the real factory state from the volume.
        assert (env.host_data / "db.sqlite3").read_text(encoding="utf-8") == "VOLUME_DB_FACTORY"

    def test_credentials_worktrees_workspaces_copied(self, tmp_path: Path) -> None:
        env = _Env(tmp_path)
        assert env.run().returncode == 0
        assert (env.host_data / "instance_id").read_text(encoding="utf-8") == "iid-123"
        assert (env.host_data / ".password-store").is_dir()
        assert (env.host_worktrees / "wt-1").read_text(encoding="utf-8") == "worktree-state"
        assert (env.host_workspaces / "ws-1").read_text(encoding="utf-8") == "workspace-state"

    def test_stack_brought_up_after_copy(self, tmp_path: Path) -> None:
        env = _Env(tmp_path)
        assert env.run().returncode == 0
        assert "up" in env.up_log.read_text(encoding="utf-8")

    def test_idempotent_rerun_archives_again_and_never_deletes(self, tmp_path: Path) -> None:
        env = _Env(tmp_path)
        assert env.run().returncode == 0
        # Second run: host DB is already the factory value; a fresh archive of it
        # is made and the first archive is untouched.
        time.sleep(1)  # timestamped archive dir is second-resolution
        assert env.run().returncode == 0
        archives = env.archives()
        assert len(archives) == 2, "each run makes its own archive; none are deleted"
        assert (archives[0] / "db.sqlite3").read_text(encoding="utf-8") == "HOST_DB_ORIGINAL"


class TestFailsLoudWhenVolumesMissing:
    def test_missing_volume_dir_is_a_clear_error(self, tmp_path: Path) -> None:
        env = _Env(tmp_path)
        shutil.rmtree(env.vol_data)
        result = env.run()
        assert result.returncode != 0
        assert "volume dir not found" in result.stderr
        # Refused before archiving.
        assert env.archives() == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
