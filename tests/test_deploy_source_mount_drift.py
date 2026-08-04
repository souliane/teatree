"""The containerized `t3` must not silently execute a tree other than the one it resolved.

``deploy/t3`` resolves ``TEATREE_SOURCE_MOUNT`` from its OWN location — the fork root
it was invoked from — so the container runs the code being edited here. That value
reaches the container only through ``compose up`` / ``compose run``. The fast path is
``compose exec`` into the ALREADY-RUNNING worker, which was created with whatever
source mount was in effect when the stack came up, and compose exec cannot change a
mount. So the resolved value is silently discarded and the CLI executes a different
tree, with nothing anywhere comparing the two.

Measured: the running stack was bind-mounting a clone 16 commits behind the checkout
the wrapper resolved. Every fix landed that day — including the fix for a bug that
destroyed a development stack — was invisible to the running system, and the wrapper
reported nothing.

The wrapper is exercised for real — the genuine ``deploy/t3`` copied into a tmp tree
and run against a ``docker`` stub. Docker is the unstoppable external.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"

SYSTEM_PATH = os.defpath.strip(os.pathsep)

pytestmark = pytest.mark.skipif(
    shutil.which("bash", path=SYSTEM_PATH) is None,
    reason="needs a system bash (present on macOS, in the deploy image, and in CI)",
)

DRIFT_MARKER = "runs a DIFFERENT source tree"


def _docker_stub(*, running: bool, container_source: str, mount_type: str = "bind") -> str:
    """A ``docker`` stub answering `compose ps` and `inspect` the way a live daemon would.

    ``inspect`` emits the ``<type> <source>`` line the wrapper's one templated call
    produces. ``compose exec`` is answered as a silent success so the wrapper's final
    hop needs no container; only what it says BEFORE dispatching is under test.
    """
    ps_reply = "echo teatree-worker-1" if running else "true"
    inspected = f"{mount_type} {container_source}".strip()
    return f"""#!/usr/bin/env bash
if [ "$1" = inspect ]; then
    printf '%s' '{inspected}'
    exit 0
fi
for arg in "$@"; do
    if [ "$arg" = ps ]; then {ps_reply}; exit 0; fi
done
exit 0
"""


def _fork_checkout(root: Path) -> Path:
    """The `<fork>/vendor/teatree/deploy/t3` layout the wrapper auto-detects."""
    entry = root / "vendor" / "teatree" / "deploy" / "t3"
    entry.parent.mkdir(parents=True)
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    (root / "pyproject.toml").write_text("[project]\nname = 'fork'\n", encoding="utf-8")
    shutil.copy2(WRAPPER, entry.parent / "docker-compose.yml")
    return entry


def _run(tmp_path: Path, stub_body: str) -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "docker"
    stub.write_text(stub_body, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    entry = _fork_checkout(tmp_path / "fork")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GITHUB_", "T3_", "TEATREE_"))}
    env["PATH"] = f"{stub_dir}{os.pathsep}{SYSTEM_PATH}"
    env["TEATREE_HOST_HOME"] = str(tmp_path / "home")

    # Stand OUTSIDE any checkout. The subject is which tree the RUNNING container
    # executes, which the wrapper decides from its own location; inheriting pytest's
    # cwd instead hands it this repo — a checkout under none of the mounts computed
    # from the redirected `TEATREE_HOST_HOME` — and the invisible-checkout refusal
    # (test_deploy_invisible_checkout_refusal.py) exits before any drift is reported.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True, exist_ok=True)

    return subprocess.run(
        [str(entry), "doctor", "check"], capture_output=True, text=True, check=True, env=env, cwd=elsewhere
    )


class TestSourceMountDrift:
    def test_a_running_container_on_a_different_tree_is_named(self, tmp_path: Path) -> None:
        stale = "/Users/someone/workspace/of-autoclone-src"
        stderr = _run(tmp_path, _docker_stub(running=True, container_source=stale)).stderr

        assert DRIFT_MARKER in stderr
        assert stale in stderr, "the tree actually being executed must be named"
        assert str(tmp_path / "fork") in stderr, "the tree the operator is editing must be named"

    def test_the_remedy_is_stated(self, tmp_path: Path) -> None:
        stderr = _run(tmp_path, _docker_stub(running=True, container_source="/elsewhere/clone")).stderr

        # A drift the operator cannot act on is just noise; name the command that ends it,
        # carrying the value whose loss caused the drift in the first place.
        assert f"TEATREE_SOURCE_MOUNT={tmp_path / 'fork'}" in stderr
        assert "up -d" in stderr
        assert str(tmp_path / "fork" / "vendor" / "teatree" / "deploy" / "docker-compose.yml") in stderr

    def test_a_container_on_the_resolved_tree_is_silent(self, tmp_path: Path) -> None:
        stub = _docker_stub(running=True, container_source=str(tmp_path / "fork"))
        assert DRIFT_MARKER not in _run(tmp_path, stub).stderr

    def test_a_docker_desktop_host_mnt_prefix_is_not_a_drift(self, tmp_path: Path) -> None:
        # Docker Desktop reports some bind sources under `/host_mnt`; comparing raw
        # would flag every mount on a Mac as drifted and make the warning worthless.
        stub = _docker_stub(running=True, container_source=f"/host_mnt{tmp_path / 'fork'}")
        assert DRIFT_MARKER not in _run(tmp_path, stub).stderr

    def test_nothing_running_is_silent(self, tmp_path: Path) -> None:
        # The one-off `run` branch creates its container FROM the resolved value,
        # so there is no divergence to report.
        stub = _docker_stub(running=False, container_source="/elsewhere/clone")
        assert DRIFT_MARKER not in _run(tmp_path, stub).stderr

    def test_a_named_volume_source_is_reported_as_drift(self, tmp_path: Path) -> None:
        # The stack came up with no source mount at all, so it runs the volume's own
        # clone — the operator's edits reach it never.
        stub = _docker_stub(running=True, container_source="", mount_type="volume")
        assert DRIFT_MARKER in _run(tmp_path, stub).stderr

    def test_an_unreadable_daemon_answer_is_silent(self, tmp_path: Path) -> None:
        # A daemon that cannot answer must not manufacture a drift the operator then
        # chases; an unknown answer is not evidence of divergence.
        stub = (
            "#!/usr/bin/env bash\n"
            'if [ "$1" = inspect ]; then exit 1; fi\n'
            'for a in "$@"; do if [ "$a" = ps ]; then echo teatree-worker-1; exit 0; fi; done\n'
            "exit 0\n"
        )
        assert DRIFT_MARKER not in _run(tmp_path, stub).stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
