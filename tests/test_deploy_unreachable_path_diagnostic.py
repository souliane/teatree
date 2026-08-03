"""The containerized `t3` must name the mount boundary, not report a phantom absence.

The container sees only the bind mounts ``deploy/t3`` sets up, so an absolute host
path outside them does not exist there — and the CLI reports it by the name it was
handed. The operator gets "No such file or directory" for a path they can ``ls`` on
the host, with nothing in the message pointing at the boundary. That has cost real
debugging time twice.

The wrapper is the only layer that knows the mapping, so it warns before dispatch.
The wrapper is exercised for real — the genuine ``deploy/t3`` copied into a tmp tree
and run against a ``docker`` stub. Docker is the unstoppable external; nothing else
is stubbed.
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

# `compose ps` answers "nothing running" so the wrapper takes its one-off `run`
# branch; that invocation succeeds silently. Only stderr is under test here.
DOCKER_STUB = """#!/usr/bin/env bash
for arg in "$@"; do
    [ "$arg" = ps ] && exit 0
done
exit 0
"""


def _write_stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(tmp_path: Path, *args: str, source_mount: Path | None = None) -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "stub-bin"
    _write_stub(stub_dir / "docker", DOCKER_STUB)

    entry = tmp_path / "teatree-deploy" / "deploy" / "t3"
    entry.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GITHUB_", "T3_", "TEATREE_"))}
    env["PATH"] = f"{stub_dir}{os.pathsep}{SYSTEM_PATH}"
    env["TEATREE_HOST_HOME"] = str(tmp_path / "home")
    if source_mount is not None:
        env["TEATREE_SOURCE_MOUNT"] = str(source_mount)

    # Stand OUTSIDE any checkout: the subject here is an unreachable path ARGUMENT,
    # and inheriting pytest's cwd would instead trip the invisible-checkout refusal
    # (this repo is a checkout, and `TEATREE_HOST_HOME` is redirected above so it
    # sits under none of the mounts the wrapper computes).
    return subprocess.run([str(entry), *args], capture_output=True, text=True, check=True, env=env, cwd=tmp_path)


class TestUnreachablePathDiagnostic:
    def test_a_path_outside_every_mount_is_named(self, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere" / "spec.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("x", encoding="utf-8")

        stderr = _run(tmp_path, "tool", "thing", str(outside)).stderr

        assert str(outside) in stderr
        assert "NOT inside any mount" in stderr

    def test_the_visible_host_roots_are_listed(self, tmp_path: Path) -> None:
        outside = tmp_path / "elsewhere" / "spec.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("x", encoding="utf-8")

        stderr = _run(tmp_path, str(outside)).stderr

        assert str(tmp_path / "home" / "workspace" / "t3-workspaces") in stderr
        assert str(tmp_path / "home" / ".local" / "share" / "teatree") in stderr

    def test_a_path_inside_a_mount_is_silent(self, tmp_path: Path) -> None:
        inside = tmp_path / "home" / "workspace" / "t3-workspaces" / "ticket" / "spec.md"
        inside.parent.mkdir(parents=True)
        inside.write_text("x", encoding="utf-8")

        assert "NOT inside any mount" not in _run(tmp_path, str(inside)).stderr

    def test_a_path_inside_the_source_mount_is_silent(self, tmp_path: Path) -> None:
        fork = tmp_path / "fork"
        inside = fork / "overlay" / "spec.md"
        inside.parent.mkdir(parents=True)
        inside.write_text("x", encoding="utf-8")

        assert "NOT inside any mount" not in _run(tmp_path, str(inside), source_mount=fork).stderr

    def test_a_nonexistent_path_is_not_claimed_unreachable(self, tmp_path: Path) -> None:
        # The wrapper only speaks about paths it can SEE. A genuinely absent path is
        # the CLI's own error to report, and claiming a mount problem would mislead.
        missing = tmp_path / "elsewhere" / "gone.md"

        assert "NOT inside any mount" not in _run(tmp_path, str(missing)).stderr

    def test_a_non_path_argument_is_ignored(self, tmp_path: Path) -> None:
        assert "NOT inside any mount" not in _run(tmp_path, "teatree", "ticket", "list", "--json").stderr

    def test_a_path_with_spaces_is_reported_whole(self, tmp_path: Path) -> None:
        outside = tmp_path / "else where" / "my spec.md"
        outside.parent.mkdir(parents=True)
        outside.write_text("x", encoding="utf-8")

        stderr = _run(tmp_path, str(outside)).stderr

        assert f"             {outside}" in stderr
