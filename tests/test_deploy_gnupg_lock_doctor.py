# test-path: cross-cutting — drives deploy/gnupg-lock-doctor.sh (no src mirror).
"""The host-side reporter for a GPG keybox lock held outside this host's namespace.

gnupg guards ``public-keys.d/pubring.db`` with a dotlock recording ``<pid>`` and
``<node>``. It reclaims a stale lock ONLY when ``<node>`` is the local node —
across a pid namespace it cannot ``kill(pid, 0)``, so it waits instead. A lock
written inside a container therefore records the CONTAINER ID, is unreclaimable
by the host BY DESIGN, and does not expire: every host ``gpg``, ``pass show`` and
signed ``git commit`` blocks until a human moves the file aside.

The reporter names that one cause behind the three unrelated-looking errors, and
never acts on it: a pid absent from this host's process table is not proof the
holder is dead, and clearing a lock held by a LIVE containerised ``keyboxd``
corrupts the keybox. Both halves are pinned here — what it reports, and that it
removes nothing.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

LOCK_DOCTOR = Path(__file__).resolve().parents[1] / "deploy" / "gnupg-lock-doctor.sh"

SH = shutil.which("sh") or "/bin/sh"
HOSTNAME = shutil.which("hostname") or "/bin/hostname"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("hostname") is None,
    reason="needs a POSIX shell and hostname",
)

FOREIGN_NODE = "1d48d78896b8"
LOCK_PID = "28533"


class TestLockDoctor:
    def _run(self, gnupg_home: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [SH, str(LOCK_DOCTOR)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(gnupg_home), "GNUPGHOME": str(gnupg_home)},
            check=False,
        )

    def _lock(self, gnupg_home: Path, node: str, pid: str = LOCK_PID) -> Path:
        lock = gnupg_home / "public-keys.d" / "pubring.db.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"{pid}\n{node}\n", encoding="utf-8")
        return lock

    def test_no_lock_is_silent(self, tmp_path: Path) -> None:
        (tmp_path / "public-keys.d").mkdir(parents=True)
        result = self._run(tmp_path)
        assert result.returncode == 0
        assert result.stderr == ""

    def test_a_local_node_lock_is_silent(self, tmp_path: Path) -> None:
        # An ordinary local holder: gnupg breaks it itself once the pid dies, so
        # reporting it would be noise on every concurrent gpg call.
        local_node = subprocess.run([HOSTNAME], capture_output=True, text=True, check=True).stdout.strip()
        self._lock(tmp_path, local_node)
        result = self._run(tmp_path)
        assert result.returncode == 0
        assert result.stderr == ""

    def test_a_foreign_node_lock_is_reported(self, tmp_path: Path) -> None:
        self._lock(tmp_path, FOREIGN_NODE)
        result = self._run(tmp_path)
        assert result.returncode == 1
        assert FOREIGN_NODE in result.stderr
        assert LOCK_PID in result.stderr

    def test_the_report_names_all_three_symptoms_as_one_cause(self, tmp_path: Path) -> None:
        # The whole point: the operator sees three unrelated-looking errors and
        # loses the session rediscovering that they share a cause.
        self._lock(tmp_path, FOREIGN_NODE)
        stderr = self._run(tmp_path).stderr
        assert "git commit" in stderr
        assert "pass show" in stderr
        assert "t3" in stderr

    def test_the_report_warns_that_an_absent_pid_is_not_a_dead_holder(self, tmp_path: Path) -> None:
        # Clearing a lock held by a LIVE containerised keyboxd corrupts the keybox.
        self._lock(tmp_path, FOREIGN_NODE)
        stderr = self._run(tmp_path).stderr
        assert "NOT proof" in stderr
        assert "docker ps" in stderr

    def test_the_remediation_names_the_container_local_home_as_the_durable_fix(self, tmp_path: Path) -> None:
        self._lock(tmp_path, FOREIGN_NODE)
        stderr = self._run(tmp_path).stderr
        assert "TEATREE_HOST_GNUPG_DIR" in stderr
        assert "/home/teatree/.gnupg-run/gnupg" in stderr

    def test_the_lock_is_never_removed(self, tmp_path: Path) -> None:
        lock = self._lock(tmp_path, FOREIGN_NODE)
        before = lock.read_text(encoding="utf-8")
        self._run(tmp_path)
        assert lock.read_text(encoding="utf-8") == before
