# test-path: cross-cutting — drives deploy/secret-decryption-probe.sh (no src mirror).
"""The host-side probe answering whether the containers decrypt secrets RIGHT NOW.

The failure it guards against is not GPG: it is a five-hour-old observation being
repeated as current. So the properties pinned here are the ones that make a stale
quote impossible — a measurement stamp, each container's own creation time, and a
per-container verdict that never generalises one service's health onto another.

Every branch runs against a stub ``docker`` on ``PATH``. That is not a convenience:
the two fault signatures (shapes disagreeing on ``GNUPGHOME``, and a shape resolving
the host keybox) cannot be provoked on a live box without a container opening the
host GPG home, which takes a cross-namespace dotlock that no host ``gpg`` can then
reclaim — the exact wedge ``deploy/gnupg-lock-doctor.sh`` exists to report.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[1] / "deploy" / "secret-decryption-probe.sh"

SH = shutil.which("sh") or "/bin/sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")

CONTAINER_HOME = "/home/teatree/.gnupg-run/gnupg"
HOST_HOME = "/home/teatree/.gnupg"
CREATED = "2026-09-02T15:20:11.737871759Z"
#: What the bare shape's `pass show` prints. The probe must discard it.
SENTINEL = "SENTINEL-PLAINTEXT-MUST-NEVER-BE-PRINTED"
MOUNTS_WITH_STORE = "/home/teatree/.password-store;/home/teatree/.gnupg;"
MOUNTS_WITHOUT_STORE = "/host-tmp;"

STUB_DOCKER = """#!/bin/sh
# Dispatches exactly the calls the probe makes, answering from files in $SCEN.
printf '%s\n' "$*" >> "$SCEN/calls"
case "$1" in
info) exit "$(cat "$SCEN/info_rc" 2>/dev/null || echo 0)" ;;
ps) cat "$SCEN/ps" 2>/dev/null; exit 0 ;;
inspect) cat "$SCEN/inspect" 2>/dev/null; exit 0 ;;
exec) ;;
*) exit 0 ;;
esac
shift
container="$1"
shift
case "$1" in
printenv) cat "$SCEN/$container.bare_home" 2>/dev/null; exit 0 ;;
timeout) printf '%s\n' "$SENTINEL"; exit "$(cat "$SCEN/$container.bare_rc" 2>/dev/null || echo 0)" ;;
esac
# `sh`: -lc is the login shape; -c is either the credential-plane probe or non-login.
flag="$2"
prog="$3"
case "$prog" in
"[ -d"*) exit "$(cat "$SCEN/$container.plane_rc" 2>/dev/null || echo 0)" ;;
esac
case "$flag" in
-lc) cat "$SCEN/$container.login" 2>/dev/null ;;
*) cat "$SCEN/$container.nonlogin" 2>/dev/null ;;
esac
exit 0
"""


@dataclass(frozen=True)
class Container:
    """One container's answers to every call the probe makes."""

    name: str
    home: str = CONTAINER_HOME
    bare_home: str | None = None
    length: int = 57
    rc: int = 0
    mounts: str = MOUNTS_WITH_STORE
    plane_rc: int = 0


class Scenario:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.bin = root / "bin"
        self.bin.mkdir(exist_ok=True)
        stub = self.bin / "docker"
        stub.write_text(STUB_DOCKER, encoding="utf-8")
        stub.chmod(0o755)
        self._names: list[str] = []
        self._inspect: list[str] = []

    def add(self, container: Container) -> None:
        name = container.name
        self._names.append(name)
        self._inspect.append(f"/{name}|{CREATED}|{container.mounts}")
        payload = f"{container.home}|{HOST_HOME}|{container.rc}|{container.length}\n"
        (self.root / f"{name}.login").write_text(payload, encoding="utf-8")
        (self.root / f"{name}.nonlogin").write_text(payload, encoding="utf-8")
        (self.root / f"{name}.bare_home").write_text(f"{container.bare_home or container.home}\n", encoding="utf-8")
        (self.root / f"{name}.bare_rc").write_text(str(container.rc), encoding="utf-8")
        (self.root / f"{name}.plane_rc").write_text(str(container.plane_rc), encoding="utf-8")

    def run(self, *args: str, daemon_down: bool = False) -> subprocess.CompletedProcess[str]:
        (self.root / "ps").write_text("".join(f"{n}\n" for n in self._names), encoding="utf-8")
        (self.root / "inspect").write_text("".join(f"{line}\n" for line in self._inspect), encoding="utf-8")
        (self.root / "info_rc").write_text("1" if daemon_down else "0", encoding="utf-8")
        return subprocess.run(
            [SH, str(PROBE), *args],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(self.root),
                "SCEN": str(self.root),
                "SENTINEL": SENTINEL,
                "TEATREE_GITLAB_TOKEN_PASS_PATH": "gitlab/pat",
                "TEATREE_SECRET_READ_DEADLINE_SECONDS": "5",
            },
            check=False,
        )


@pytest.fixture
def scenario(tmp_path: Path) -> Scenario:
    return Scenario(tmp_path / "scen")


class TestHealthy:
    def test_all_shapes_decrypting_is_green(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1"))
        scenario.add(Container("teatree-teatree-admin-1"))
        result = scenario.run()

        assert result.returncode == 0, result.stdout
        assert "VERDICT: GREEN" in result.stdout

    def test_every_container_is_reported_on_its_own_line(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1"))
        scenario.add(Container("teatree-teatree-admin-1"))
        result = scenario.run()

        assert "teatree-teatree-worker-1" in result.stdout
        assert "teatree-teatree-admin-1" in result.stdout

    def test_output_carries_a_measurement_stamp_and_the_creation_time(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1"))
        result = scenario.run()

        assert "measured 20" in result.stdout
        assert "2026-09-02T15:20:11Z" in result.stdout, "the container's own creation time"
        assert "nothing later" in result.stdout, "the answer must refuse to be quoted forward"

    def test_plaintext_is_discarded_and_only_a_length_is_reported(self, scenario: Scenario) -> None:
        """The bare shape is the one venue where plaintext could reach the host."""
        scenario.add(Container("teatree-teatree-worker-1"))
        result = scenario.run()

        assert "len=57" in result.stdout
        assert SENTINEL not in result.stdout
        assert SENTINEL not in result.stderr

    def test_bare_exec_is_bounded_inside_the_container_without_a_shell(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1"))
        result = scenario.run()

        calls = (scenario.root / "calls").read_text(encoding="utf-8")
        assert result.returncode == 0, result.stdout
        assert "exec teatree-teatree-worker-1 timeout 5 pass show gitlab/pat" in calls


class TestFailsLoud:
    def test_a_named_container_that_does_not_exist_is_red(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1"))
        result = scenario.run("teatree-teatree-worker-1", "ghost-container")

        assert result.returncode == 1
        assert "no such container" in result.stdout

    def test_one_healthy_container_does_not_carry_the_verdict(self, scenario: Scenario) -> None:
        """The incident in one line: a healthy worker was read as a healthy admin."""
        scenario.add(Container("teatree-teatree-worker-1"))
        scenario.add(Container("teatree-teatree-admin-1", rc=2))
        result = scenario.run()

        assert result.returncode == 1
        assert "VERDICT: RED" in result.stdout
        assert "OK len=57" in result.stdout, "the healthy one is still reported healthy"

    def test_rc_zero_with_an_empty_answer_is_the_silent_failure(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1", rc=0, length=0))
        result = scenario.run()

        assert result.returncode == 1
        assert "EMPTILY" in result.stdout

    def test_an_unreachable_daemon_measures_nothing_and_says_so(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1"))
        result = scenario.run(daemon_down=True)

        assert result.returncode == 1
        assert "NOTHING was measured" in result.stdout

    def test_an_empty_container_set_is_not_a_pass(self, scenario: Scenario) -> None:
        result = scenario.run()

        assert result.returncode == 1
        assert "UNPROVEN" in result.stdout


class TestFaultSignatures:
    def test_shapes_disagreeing_on_gnupghome_is_red_even_while_decrypting(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1", bare_home="/home/teatree/.gnupg-other", rc=0))
        result = scenario.run()

        assert result.returncode == 1
        assert "DIVERGENT" in result.stdout

    def test_a_shape_resolving_the_host_keybox_is_red(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1", home=HOST_HOME, bare_home=HOST_HOME))
        result = scenario.run()

        assert result.returncode == 1
        assert "HOST-HOME" in result.stdout


class TestCredentialPlaneExclusion:
    def test_a_container_without_a_store_is_excluded_not_failed(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-worker-1"))
        scenario.add(Container("teatree-teatree-watchdog-1", mounts=MOUNTS_WITHOUT_STORE, plane_rc=1))
        result = scenario.run()

        assert result.returncode == 0, result.stdout
        assert "no credential plane" in result.stdout
        assert "1 excluded" in result.stdout

    def test_excluding_every_container_proves_nothing_and_is_red(self, scenario: Scenario) -> None:
        scenario.add(Container("teatree-teatree-watchdog-1", mounts=MOUNTS_WITHOUT_STORE, plane_rc=1))
        result = scenario.run()

        assert result.returncode == 1
        assert "Nothing was probed" in result.stdout

    def test_a_declared_mount_wins_over_a_runtime_miss(self, scenario: Scenario) -> None:
        """A store baked into an image declares no mount, so the runtime check decides."""
        scenario.add(Container("teatree-teatree-worker-1", mounts=MOUNTS_WITHOUT_STORE, plane_rc=0))
        result = scenario.run()

        assert result.returncode == 0, result.stdout
        assert "OK len=57" in result.stdout
