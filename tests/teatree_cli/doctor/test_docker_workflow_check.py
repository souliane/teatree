"""Tests for the containerized-``t3`` doctor gates (#3232).

``_check_t3_launcher_managed`` and ``_check_control_db_reachable`` are the two
ways the container-only CLI reverts to something that resolves config from
shipped defaults, so each is exercised in BOTH directions: it fires on every
broken shape, and it is silent on every healthy one.

"Healthy" includes NEVER SET UP. Both gates report drift, so a native clone with
no launcher and no control-DB volume must pass — a gate that fires there can never
let ``t3 doctor check`` exit 0 on a checkout of core. Each never-set-up case below
is paired with the drift case it must still catch.

The launcher gate runs on either side of the container boundary — a host reads
its own ``PATH``, a container reads the host's through the bind mount — because
the container is the only venue ``t3`` still has while the launcher it verifies
lives on the host.
"""

import io
import os
from contextlib import redirect_stdout
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli.doctor import checks_docker
from teatree.cli.doctor.checks_docker import _check_control_db_reachable, _check_t3_launcher_managed
from teatree.docker.workflow import install_launcher, is_running_in_container, launcher_path, wrapper_path
from teatree.paths import CONTROL_DB_DIR_ENV

# A container-marker path guaranteed absent, so container detection in these
# host-scenario checks keys ONLY off the injected env — never the real
# ``/.dockerenv`` marker. That marker EXISTS whenever the suite itself runs inside
# the CI test container (the shard/coverage lanes run pytest under `docker run`),
# which would otherwise make every host check early-return "in a container".
_ABSENT_DOCKERENV = Path("/nonexistent/teatree-test/.dockerenv")


def _run_launcher_check(**kwargs) -> tuple[bool, str]:
    out = io.StringIO()
    hermetic = partial(is_running_in_container, dockerenv=_ABSENT_DOCKERENV)
    with patch.object(checks_docker, "is_running_in_container", hermetic), redirect_stdout(out):
        ok = _check_t3_launcher_managed(**kwargs)
    return ok, out.getvalue()


def _run_control_db_check(env: dict[str, str]) -> tuple[bool, str]:
    out = io.StringIO()
    hermetic = partial(is_running_in_container, dockerenv=_ABSENT_DOCKERENV)
    with patch.object(checks_docker, "is_running_in_container", hermetic), redirect_stdout(out):
        ok = _check_control_db_reachable(env=env)
    return ok, out.getvalue()


def _live_checkout(root: Path) -> Path:
    """A checkout whose ``deploy/t3`` exists and is executable, as a host sees it."""
    checkout = root / "clone"
    wrapper = wrapper_path(checkout)
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    wrapper.chmod(0o755)
    return checkout


class TestLauncherGateOnAHost:
    def test_passes_when_path_t3_is_a_managed_launcher_for_a_live_checkout(self, tmp_path: Path) -> None:
        checkout = _live_checkout(tmp_path)
        launcher = tmp_path / "bin" / "t3"
        install_launcher(launcher, checkout)

        ok, msg = _run_launcher_check(env={"HOME": str(tmp_path)}, which=lambda _t: str(launcher))
        assert ok is True
        assert msg == ""

    def test_fails_when_a_uv_console_script_took_the_name_back(self, tmp_path: Path) -> None:
        install_launcher(launcher_path({"HOME": str(tmp_path)}), _live_checkout(tmp_path))
        uv_script = tmp_path / ".local" / "share" / "uv" / "tools" / "teatree" / "bin" / "t3"
        uv_script.parent.mkdir(parents=True)
        uv_script.write_text("#!/usr/bin/env python\n", encoding="utf-8")

        ok, msg = _run_launcher_check(env={"HOME": str(tmp_path)}, which=lambda _t: str(uv_script))
        assert ok is False
        assert "FAIL" in msg
        assert str(uv_script) in msg
        assert "t3 setup" in msg

    def test_fails_when_an_installed_launcher_is_absent_from_path(self, tmp_path: Path) -> None:
        # Drift with nothing shadowing it: the launcher is installed, but the shell that
        # runs `t3` no longer resolves it — so `t3` reaches teatree by some other route.
        install_launcher(launcher_path({"HOME": str(tmp_path)}), _live_checkout(tmp_path))

        ok, msg = _run_launcher_check(env={"HOME": str(tmp_path)}, which=lambda _t: None)
        assert ok is False
        assert "FAIL" in msg
        assert "<nothing>" in msg

    def test_silent_when_no_launcher_was_ever_installed(self, tmp_path: Path) -> None:
        # The never-set-up case: a native clone of core has no launcher to shadow, and a
        # gate firing here would keep `t3 doctor check` from ever exiting 0 on one.
        ok, msg = _run_launcher_check(env={"HOME": str(tmp_path)}, which=lambda _t: None)
        assert ok is True
        assert msg == ""

    def test_silent_when_something_else_owns_t3_and_no_launcher_was_installed(self, tmp_path: Path) -> None:
        # The foil for the shadowing case above: the SAME PATH shape, minus the installed
        # launcher, is how a `uv tool install --editable .` checkout of core legitimately looks.
        uv_script = tmp_path / ".local" / "share" / "uv" / "tools" / "teatree" / "bin" / "t3"
        uv_script.parent.mkdir(parents=True)
        uv_script.write_text("#!/usr/bin/env python\n", encoding="utf-8")

        ok, msg = _run_launcher_check(env={"HOME": str(tmp_path)}, which=lambda _t: str(uv_script))
        assert ok is True
        assert msg == ""

    def test_fails_when_the_launcher_names_a_checkout_that_moved(self, tmp_path: Path) -> None:
        gone = tmp_path / "relocated-away"
        launcher = tmp_path / "bin" / "t3"
        install_launcher(launcher, gone)

        ok, msg = _run_launcher_check(env={"HOME": str(tmp_path)}, which=lambda _t: str(launcher))
        assert ok is False
        assert str(wrapper_path(gone)) in msg
        assert "moved or was deleted" in msg
        assert "t3 setup" in msg


class TestLauncherGateInsideTheContainer:
    def _mount_with(self, tmp_path: Path, checkout: Path | None) -> Path:
        mount = tmp_path / "host-bin"
        mount.mkdir()
        if checkout is not None:
            install_launcher(mount / "t3", checkout)
        return mount

    def test_passes_when_the_mounted_host_launcher_names_this_checkout(self, tmp_path: Path) -> None:
        checkout = Path("/nonexistent/t3-fixture/current-checkout")
        mount = self._mount_with(tmp_path, checkout)

        ok, msg = _run_launcher_check(
            env={"TEATREE_ROLE": "worker", "TEATREE_DEPLOY_CHECKOUT": str(checkout)},
            mount_dir=mount,
        )
        assert ok is True
        assert msg == ""

    def test_fails_when_the_mounted_host_launcher_names_a_stale_checkout(self, tmp_path: Path) -> None:
        stale = Path("/nonexistent/t3-fixture/old-checkout")
        current = Path("/nonexistent/t3-fixture/new-checkout")
        mount = self._mount_with(tmp_path, stale)

        ok, msg = _run_launcher_check(
            env={"TEATREE_ROLE": "worker", "TEATREE_DEPLOY_CHECKOUT": str(current)},
            mount_dir=mount,
        )
        assert ok is False
        assert str(wrapper_path(stale)) in msg
        assert str(wrapper_path(current)) in msg
        assert "t3 setup" in msg

    def test_fails_when_the_host_t3_is_not_a_managed_launcher(self, tmp_path: Path) -> None:
        mount = self._mount_with(tmp_path, None)
        (mount / "t3").write_text("#!/usr/bin/env python\n", encoding="utf-8")

        ok, msg = _run_launcher_check(
            env={"TEATREE_ROLE": "worker", "TEATREE_DEPLOY_CHECKOUT": "/nonexistent/t3-fixture/current-checkout"},
            mount_dir=mount,
        )
        assert ok is False
        assert "not the managed container launcher" in msg

    def test_silent_without_the_mount(self, tmp_path: Path) -> None:
        # A container deploy/t3 did not start carries no window onto the host and
        # must not invent a verdict about it.
        ok, msg = _run_launcher_check(env={"TEATREE_ROLE": "worker"}, mount_dir=tmp_path / "absent")
        assert ok is True
        assert msg == ""


class TestControlDbReachableCheck:
    def test_a_container_with_no_volume_mounted_is_a_finding(self, tmp_path: Path) -> None:
        # Why every doctor test stages T3_CONTROL_DB_DIR: a test runner is a container
        # with no volume, so unstaged this one finding turns a whole `doctor check` red
        # on a fact about the runner rather than about the tree under test.
        out = io.StringIO()
        with patch.object(checks_docker, "is_running_in_container", lambda *_a, **_k: True), redirect_stdout(out):
            ok = _check_control_db_reachable(env={"T3_CONTROL_DB_DIR": str(tmp_path / "absent")})
        assert ok is False
        assert "does not exist" in out.getvalue()

    def test_passes_when_the_control_db_directory_is_readable(self, tmp_path: Path) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            ok = _check_control_db_reachable(env={"T3_CONTROL_DB_DIR": str(tmp_path)})
        assert ok is True
        assert out.getvalue() == ""

    def test_passes_for_a_native_checkout_pointed_at_its_own_database(self, tmp_path: Path) -> None:
        # The invariant is readability, not being the container's named volume: a
        # dev checkout resolving its own real control DB is healthy.
        own = tmp_path / "var" / "lib" / "teatree" / "control-db"
        own.mkdir(parents=True)
        (own / "db.sqlite3").write_bytes(b"")
        out = io.StringIO()
        with redirect_stdout(out):
            ok = _check_control_db_reachable(env={"T3_CONTROL_DB_DIR": str(own)})
        assert ok is True
        assert out.getvalue() == ""

    def test_fails_when_the_control_db_directory_is_absent(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-volume-here"
        out = io.StringIO()
        with redirect_stdout(out):
            ok = _check_control_db_reachable(env={"T3_CONTROL_DB_DIR": str(missing)})
        assert ok is False
        message = out.getvalue()
        assert "FAIL" in message
        assert str(missing) in message
        assert "does not exist" in message
        assert "shipped defaults" in message

    def test_fails_when_the_control_db_directory_is_unreadable(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:  # pragma: no cover — root bypasses the mode bits entirely
            pytest.skip("root reads any directory regardless of its mode")
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o000)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                ok = _check_control_db_reachable(env={"T3_CONTROL_DB_DIR": str(locked)})
            assert ok is False
            assert "is not readable" in out.getvalue()
        finally:
            locked.chmod(0o700)

    def test_absent_volume_is_not_a_finding_when_nothing_uses_it(self, tmp_path: Path) -> None:
        """A native clone that named no database has nothing to repair.

        The default resolves to the container's named volume, so FAILing here would
        leave `t3 doctor check` unable to exit 0 on any host checkout.
        """
        absent_volume = tmp_path / "var" / "lib" / "teatree" / "control-db"
        with patch("teatree.paths.DEFAULT_CONTROL_DB_DIR", absent_volume):
            ok, output = _run_control_db_check(env={})
        assert ok is True
        assert "FAIL" not in output

    def test_absent_volume_fails_inside_the_container_that_mounts_it(self, tmp_path: Path) -> None:
        absent_volume = tmp_path / "var" / "lib" / "teatree" / "control-db"
        with patch("teatree.paths.DEFAULT_CONTROL_DB_DIR", absent_volume):
            ok, output = _run_control_db_check(env={"TEATREE_ROLE": "worker"})
        assert ok is False
        assert str(absent_volume) in output

    def test_absent_directory_fails_when_the_env_names_it(self, tmp_path: Path) -> None:
        named = tmp_path / "named" / "control-db"
        ok, output = _run_control_db_check(env={CONTROL_DB_DIR_ENV: str(named)})
        assert ok is False
        assert str(named) in output


class TestBothGatesFeedTheDoctorExitCode:
    """A verdict not assigned into ``ok`` can never redden the run."""

    def test_both_new_checks_are_wired_as_gates(self) -> None:
        import ast  # noqa: PLC0415 — deferred: only this structural assertion needs it
        import inspect  # noqa: PLC0415 — deferred: only this structural assertion needs it
        import textwrap  # noqa: PLC0415 — deferred: only this structural assertion needs it

        from teatree.cli.doctor import app  # noqa: PLC0415 — deferred: heavy CLI import at call time

        tree = ast.parse(textwrap.dedent(inspect.getsource(app.run_doctor_checks)))
        gating = {
            call.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ok" for t in node.targets)
            for call in ast.walk(node.value)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        assert {"_check_t3_launcher_managed", "_check_control_db_reachable"} <= gating
