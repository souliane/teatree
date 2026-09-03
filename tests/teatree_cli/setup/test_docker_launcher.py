"""Tests for ``DockerLauncherInstaller`` — the ``t3 setup`` launcher unit (#3232).

``t3`` on the host is one fixed thing: an executable launcher on ``PATH`` that
``exec``s the main checkout's ``deploy/t3``. These cover the install policy on
both sides of the container boundary (a host writes its own ``PATH``, a container
writes the host's through the bind mount), the uv-tool retirement behind it, and
the launcher's own runtime behaviour — argument, stdin, stderr and exit-code
fidelity, cwd independence, and the non-zero refusal when Docker is absent.
"""

import os
import shutil
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

import teatree
from teatree.cli.setup import docker_launcher
from teatree.cli.setup.docker_launcher import DockerLauncherInstaller
from teatree.docker import workflow
from teatree.docker.workflow import (
    is_managed_launcher,
    is_running_in_container,
    launcher_wrapper_target,
    read_managed_launcher,
    wrapper_path,
)

# A container-marker path guaranteed absent, so container detection keys ONLY off
# the injected env — the real ``/.dockerenv`` exists whenever the suite itself runs
# in the CI test container, which would make every host scenario below no-op.
_ABSENT_DOCKERENV = Path("/nonexistent/teatree-test/.dockerenv")

_HOST = partial(is_running_in_container, dockerenv=_ABSENT_DOCKERENV)


class _FakeUv:
    """Stand-in for the ``uv`` subprocess calls the retire step makes."""

    def __init__(self, tools_dir: Path | None, *, uninstall_rc: int = 0) -> None:
        self.tools_dir = tools_dir
        self.uninstall_rc = uninstall_rc
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], cwd: Path | None = None) -> CompletedProcess[str]:
        self.calls.append(args)
        if args[1:] == ["tool", "dir"]:
            stdout = f"{self.tools_dir}\n" if self.tools_dir is not None else ""
            return CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        return CompletedProcess(args=args, returncode=self.uninstall_rc, stdout="", stderr="tool not found")

    @property
    def uninstalled(self) -> bool:
        return any(call[1:] == ["tool", "uninstall", "teatree"] for call in self.calls)


def _installed_uv_tools_dir(home: Path) -> Path:
    """A uv tools root that reports ``teatree`` as installed."""
    tools = home / ".local" / "share" / "uv" / "tools"
    (tools / "teatree").mkdir(parents=True, exist_ok=True)
    return tools


def _drive(installer: DockerLauncherInstaller, fake_uv: _FakeUv) -> list[str]:
    """Run *installer* against the uv stand-in, returning the lines it echoed."""
    messages: list[str] = []
    with (
        patch.object(docker_launcher, "is_running_in_container", _HOST),
        patch.object(docker_launcher, "run_captured", fake_uv),
    ):
        installer.install(echo=messages.append)
    return messages


def _run_install(
    repo: Path,
    home: Path,
    *,
    uv: _FakeUv | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[list[str], _FakeUv]:
    """Install into *home* as a HOST would, returning the lines and the uv stand-in."""
    fake_uv = uv if uv is not None else _FakeUv(None)
    launcher = home / ".local" / "bin" / "t3"
    resolve = which if which is not None else (lambda tool: str(launcher) if tool == "t3" else "/usr/bin/uv")
    installer = DockerLauncherInstaller(repo, env={"HOME": str(home)}, which=resolve)
    return _drive(installer, fake_uv), fake_uv


class TestLauncherInstall:
    def test_writes_an_executable_launcher_pointing_at_the_main_checkout(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        messages, _uv = _run_install(repo, home)

        launcher = home / ".local" / "bin" / "t3"
        assert is_managed_launcher(launcher)
        assert os.access(launcher, os.X_OK)
        assert str(wrapper_path(repo)) in launcher.read_text(encoding="utf-8")
        assert any(m.startswith("OK") and "Installed the containerized t3 launcher" in m for m in messages)

    def test_rerun_is_idempotent_and_warns_about_nothing(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        _run_install(repo, home)
        before = (home / ".local" / "bin" / "t3").read_bytes()

        messages, _uv = _run_install(repo, home)
        assert any(m.startswith("OK") and "already current" in m for m in messages)
        assert not any(m.startswith("WARN") for m in messages)
        assert (home / ".local" / "bin" / "t3").read_bytes() == before

    def test_replaces_the_uv_console_script_symlink(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        uv_script = home / ".local" / "share" / "uv" / "tools" / "teatree" / "bin" / "t3"
        uv_script.parent.mkdir(parents=True)
        uv_script.write_text("#!/usr/bin/env python\n", encoding="utf-8")
        launcher = home / ".local" / "bin" / "t3"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(uv_script)

        messages, _uv = _run_install(repo, home)
        assert not launcher.is_symlink()
        assert is_managed_launcher(launcher)
        assert any("Repointed the t3 launcher" in m for m in messages)

    def test_repoints_a_relocated_checkout_and_keeps_setup_going(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        _run_install(tmp_path / "old-clone", home)
        messages, _uv = _run_install(tmp_path / "new-clone", home)

        launcher = home / ".local" / "bin" / "t3"
        assert launcher_wrapper_target(launcher.read_text(encoding="utf-8")) == wrapper_path(tmp_path / "new-clone")
        assert any(m.startswith("OK") and "Repointed" in m for m in messages)

    def test_refuses_an_unmanaged_file_and_names_the_manual_fix(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        launcher = home / ".local" / "bin" / "t3"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

        messages, fake_uv = _run_install(repo, home)
        assert launcher.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
        warning = next(m for m in messages if m.startswith("WARN"))
        assert f"mv {launcher} {launcher}.bak" in warning
        assert "t3 setup" in warning
        assert not fake_uv.uninstalled

    def test_an_unverified_publish_warns_and_leaves_the_uv_tool_alone(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        fake_uv = _FakeUv(_installed_uv_tools_dir(home))
        with patch.object(workflow.os, "replace", lambda *_args: None):
            messages, fake_uv = _run_install(repo, home, uv=fake_uv)

        assert any(m.startswith("WARN") and "did not read back" in m for m in messages)
        assert not fake_uv.uninstalled


class TestContainerWritesTheHostLauncher:
    """The container is the only runtime left, so it repairs the host launcher."""

    def _run_in_container(
        self, tmp_path: Path, *, mount: Path | None, checkout: Path | None
    ) -> tuple[list[str], _FakeUv]:
        env = {"HOME": str(tmp_path / "container-home"), "TEATREE_ROLE": "worker"}
        if checkout is not None:
            env["TEATREE_DEPLOY_CHECKOUT"] = str(checkout)
        # `uv` DOES resolve in the image, so a retirement attempt would reach the
        # stand-in — which is what makes the "never touched" assertions below real
        # rather than satisfied by an unresolvable tool.
        fake_uv = _FakeUv(_installed_uv_tools_dir(tmp_path / "container-home"))
        installer = DockerLauncherInstaller(
            tmp_path / "container-clone",
            env=env,
            which=lambda tool: "/opt/teatree/uv/bin/t3" if tool == "t3" else "/opt/teatree/uv/bin/uv",
            host_bin_mount=mount if mount is not None else tmp_path / "no-such-mount",
        )
        return _drive(installer, fake_uv), fake_uv

    def test_installs_the_host_launcher_through_the_mount(self, tmp_path: Path) -> None:
        mount = tmp_path / "host-bin"
        mount.mkdir()
        host_checkout = Path("/nonexistent/t3-fixture/current-checkout")

        messages, fake_uv = self._run_in_container(tmp_path, mount=mount, checkout=host_checkout)

        launcher = mount / "t3"
        assert launcher_wrapper_target(launcher.read_text(encoding="utf-8")) == wrapper_path(host_checkout)
        assert os.access(launcher, os.X_OK)
        assert any(m.startswith("OK") and "Installed the containerized t3 launcher" in m for m in messages)
        # The uv tool registry lives on the host, out of reach — never touched here.
        assert fake_uv.calls == []

    def test_repoints_the_host_launcher_when_the_checkout_moved(self, tmp_path: Path) -> None:
        mount = tmp_path / "host-bin"
        mount.mkdir()
        self._run_in_container(tmp_path, mount=mount, checkout=Path("/nonexistent/t3-fixture/old-checkout"))
        messages, _uv = self._run_in_container(
            tmp_path, mount=mount, checkout=Path("/nonexistent/t3-fixture/new-checkout")
        )

        script = (mount / "t3").read_text(encoding="utf-8")
        assert launcher_wrapper_target(script) == wrapper_path(Path("/nonexistent/t3-fixture/new-checkout"))
        assert any(m.startswith("OK") and "Repointed" in m for m in messages)

    def test_no_mount_leaves_the_host_alone_with_a_stated_reason(self, tmp_path: Path) -> None:
        messages, fake_uv = self._run_in_container(tmp_path, mount=None, checkout=tmp_path / "checkout")

        assert any(m.startswith("OK") and "No host bin mount" in m for m in messages)
        assert not (tmp_path / "container-home" / ".local" / "bin" / "t3").exists()
        assert fake_uv.calls == []

    def test_a_mount_with_no_named_checkout_warns_rather_than_guessing(self, tmp_path: Path) -> None:
        mount = tmp_path / "host-bin"
        mount.mkdir()
        messages, fake_uv = self._run_in_container(tmp_path, mount=mount, checkout=None)

        assert any(m.startswith("WARN") and "names no host checkout" in m for m in messages)
        assert not (mount / "t3").exists()
        assert fake_uv.calls == []

    def test_refuses_an_unmanaged_host_t3_through_the_mount(self, tmp_path: Path) -> None:
        mount = tmp_path / "host-bin"
        mount.mkdir()
        (mount / "t3").write_text("#!/bin/sh\necho operator's own\n", encoding="utf-8")

        messages, fake_uv = self._run_in_container(tmp_path, mount=mount, checkout=tmp_path / "checkout")
        assert (mount / "t3").read_text(encoding="utf-8") == "#!/bin/sh\necho operator's own\n"
        assert any(m.startswith("WARN") and "not a teatree-managed t3" in m for m in messages)
        assert fake_uv.calls == []


class TestWiredIntoSetup:
    def test_t3_setup_installs_the_launcher_and_retires_the_alias(self) -> None:
        from teatree.cli.setup.command import run  # noqa: PLC0415 — deferred: heavy CLI import at call time

        assert "DockerLauncherInstaller" in run.__code__.co_names
        assert "retire_alias" in run.__code__.co_names


class TestHostToolRetirement:
    def test_uninstalls_the_uv_tool_after_a_verified_launcher(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        fake_uv = _FakeUv(_installed_uv_tools_dir(home))
        messages, fake_uv = _run_install(repo, home, uv=fake_uv)

        assert fake_uv.uninstalled
        assert any("Removed the uv-installed host t3" in m for m in messages)
        assert is_managed_launcher(home / ".local" / "bin" / "t3")

    def test_a_refused_launcher_leaves_the_uv_tool_alone(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        launcher = home / ".local" / "bin" / "t3"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_uv = _FakeUv(_installed_uv_tools_dir(home))

        _messages, fake_uv = _run_install(repo, home, uv=fake_uv)
        assert not fake_uv.uninstalled

    def test_an_unwritable_launcher_leaves_the_uv_tool_alone(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        # A file where the bin DIRECTORY belongs makes the launcher write fail.
        (home / ".local").mkdir(parents=True)
        (home / ".local" / "bin").write_text("", encoding="utf-8")
        fake_uv = _FakeUv(_installed_uv_tools_dir(home))

        messages, fake_uv = _run_install(repo, home, uv=fake_uv)
        assert any("not writable" in m for m in messages)
        assert not fake_uv.uninstalled

    def test_a_launcher_that_path_does_not_resolve_leaves_the_uv_tool_alone(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        fake_uv = _FakeUv(_installed_uv_tools_dir(home))
        shadow = tmp_path / "elsewhere" / "t3"

        messages, fake_uv = _run_install(
            repo,
            home,
            uv=fake_uv,
            which=lambda tool: str(shadow) if tool == "t3" else "/usr/bin/uv",
        )
        assert not fake_uv.uninstalled
        assert any(m.startswith("WARN") and "ahead of any other" in m for m in messages)

    def test_nothing_installed_is_quiet_on_rerun(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        messages, fake_uv = _run_install(repo, home, uv=_FakeUv(tmp_path / "empty-uv-tools"))

        assert not fake_uv.uninstalled
        retire = next(m for m in messages if "uv-installed host t3" in m)
        assert retire.startswith("OK")

    def test_a_failing_uninstall_warns_and_continues(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        fake_uv = _FakeUv(_installed_uv_tools_dir(home), uninstall_rc=2)

        messages, fake_uv = _run_install(repo, home, uv=fake_uv)
        assert fake_uv.uninstalled
        assert any(m.startswith("WARN") and "tool uninstall teatree" in m for m in messages)

    def test_missing_uv_warns_and_continues(self, tmp_path: Path) -> None:
        repo, home = tmp_path / "clone", tmp_path / "home"
        launcher = home / ".local" / "bin" / "t3"
        messages, fake_uv = _run_install(
            repo,
            home,
            which=lambda tool: str(launcher) if tool == "t3" else None,
        )
        assert fake_uv.calls == []
        assert any(m.startswith("WARN") and "`uv` not on PATH" in m for m in messages)


def _bin_with(tools: list[str], destination: Path) -> Path:
    """A PATH directory carrying only *tools*, so anything else is unresolvable."""
    destination.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        found = shutil.which(tool)
        if found is None:  # pragma: no cover — every supported host has these
            pytest.skip(f"{tool} is not on PATH")
        (destination / tool).symlink_to(found)
    return destination


# Reports argv one line at a time, echoes stdin, writes to stderr, and exits
# non-zero — so every channel the launcher must pass through unchanged is visible.
_ECHOING_WRAPPER = """#!/usr/bin/env bash
for arg in "$@"; do printf 'ARG[%s]\\n' "$arg"; done
printf 'STDIN[%s]\\n' "$(cat)"
printf 'ERR[%s]\\n' "$#" >&2
exit 7
"""


class TestLauncherRuntimeBehaviour:
    """The installed file's own behaviour, executed by a real shell."""

    def _install_against_stub_wrapper(self, tmp_path: Path, script: str = _ECHOING_WRAPPER) -> Path:
        repo, home = tmp_path / "clone", tmp_path / "home"
        wrapper = wrapper_path(repo)
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(script, encoding="utf-8")
        wrapper.chmod(0o755)
        _run_install(repo, home)
        return home / ".local" / "bin" / "t3"

    def test_passes_arguments_stdin_stderr_and_the_exit_code_through_unchanged(self, tmp_path: Path) -> None:
        launcher = self._install_against_stub_wrapper(tmp_path)
        argv = ["review", "post-comment", "--", "a message with spaces", "--file=src/x y.py", "-"]

        result = subprocess.run(
            [str(launcher), *argv],
            input="piped payload",
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 7
        assert result.stdout.splitlines()[: len(argv)] == [f"ARG[{arg}]" for arg in argv]
        assert "STDIN[piped payload]" in result.stdout
        assert result.stderr.strip() == f"ERR[{len(argv)}]"

    def test_resolves_the_same_checkout_from_any_cwd(self, tmp_path: Path) -> None:
        launcher = self._install_against_stub_wrapper(tmp_path, '#!/usr/bin/env bash\necho "wrapper=$0 args=$*"\n')
        here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
        here.mkdir()
        elsewhere.mkdir()

        outputs = [
            subprocess.run([str(launcher), "info"], cwd=cwd, capture_output=True, text=True, check=True).stdout
            for cwd in (here, elsewhere)
        ]
        assert outputs[0] == outputs[1]
        assert str(wrapper_path(tmp_path / "clone")) in outputs[0]

    def test_exits_non_zero_naming_the_repair_when_docker_is_absent(self, tmp_path: Path) -> None:
        repo = Path(teatree.__file__).resolve().parents[2]
        home = tmp_path / "home"
        _run_install(repo, home)
        launcher = home / ".local" / "bin" / "t3"

        dockerless = _bin_with(["bash", "dirname"], tmp_path / "dockerless-bin")
        result = subprocess.run(
            [str(launcher), "info"],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": str(dockerless), "HOME": str(home)},
        )
        assert result.returncode != 0
        assert "docker" in result.stderr.lower()
        assert result.stdout == ""

    def test_the_installed_launcher_is_the_managed_one_it_reports(self, tmp_path: Path) -> None:
        launcher = self._install_against_stub_wrapper(tmp_path)
        script = read_managed_launcher(launcher)
        assert script is not None
        assert launcher_wrapper_target(script) == wrapper_path(tmp_path / "clone")
