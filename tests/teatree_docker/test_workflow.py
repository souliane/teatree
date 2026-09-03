"""Tests for the containerized-``t3`` workflow pure logic (#3232).

The launcher script, its install policy, the checkout it names, and the retirement
of the superseded alias block are shared by ``t3 setup`` (the installer) and
``t3 doctor`` (the verifier), so they are covered here in one place.

The install policy is the load-bearing half: ``t3`` is the operator's only entry
point, so a write that tears, half-lands, or silently reports success it did not
achieve leaves the machine with no working CLI. Those properties are asserted
through observable behaviour — a reader holding the live file, a failed rename, a
read-back that disagrees — never through the shape of the implementation.
"""

import os
import threading
from pathlib import Path
from unittest.mock import patch

from teatree.docker import workflow
from teatree.docker.workflow import (
    ALIAS_MARKER_BEGIN,
    ALIAS_MARKER_END,
    LAUNCHER_MARKER,
    AliasRemoval,
    LauncherInstall,
    install_launcher,
    is_managed_launcher,
    is_running_in_container,
    launcher_bin_dir,
    launcher_path,
    launcher_wrapper_target,
    read_managed_launcher,
    remove_alias_block,
    render_launcher_script,
    wrapper_path,
)

_USER_RC = """# the operator's own profile
greet() {
    echo hello
}

"""
_USER_RC_TAIL = """export EDITOR=emacs
farewell() {
    echo bye
}
"""
_MANAGED_ALIAS_BLOCK = f'{ALIAS_MARKER_BEGIN}\nalias t3="/somewhere/deploy/t3"\n{ALIAS_MARKER_END}\n'


class TestIsRunningInContainer:
    def test_true_when_teatree_role_set(self) -> None:
        assert is_running_in_container({"TEATREE_ROLE": "worker"}, dockerenv=Path("/nope")) is True

    def test_true_when_dockerenv_marker_present(self, tmp_path: Path) -> None:
        marker = tmp_path / ".dockerenv"
        marker.write_text("", encoding="utf-8")
        assert is_running_in_container({}, dockerenv=marker) is True

    def test_false_on_a_plain_host(self, tmp_path: Path) -> None:
        assert is_running_in_container({}, dockerenv=tmp_path / "absent") is False


class TestRemoveAliasBlock:
    """Only the fenced block goes; the operator's own rc content is untouched."""

    def _rc_with_block(self, tmp_path: Path) -> Path:
        rc = tmp_path / ".zshrc"
        rc.write_text(_USER_RC + _MANAGED_ALIAS_BLOCK + _USER_RC_TAIL, encoding="utf-8")
        return rc

    def test_surrounding_user_content_is_byte_identical_afterwards(self, tmp_path: Path) -> None:
        rc = self._rc_with_block(tmp_path)
        assert remove_alias_block(rc) is AliasRemoval.REMOVED
        assert rc.read_text(encoding="utf-8") == _USER_RC + _USER_RC_TAIL

    def test_rerun_reports_absent_and_changes_nothing(self, tmp_path: Path) -> None:
        rc = self._rc_with_block(tmp_path)
        remove_alias_block(rc)
        after_first = rc.read_bytes()
        assert remove_alias_block(rc) is AliasRemoval.ABSENT
        assert rc.read_bytes() == after_first

    def test_an_rc_without_the_markers_is_left_alone(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        rc.write_text(_USER_RC, encoding="utf-8")
        assert remove_alias_block(rc) is AliasRemoval.ABSENT
        assert rc.read_text(encoding="utf-8") == _USER_RC

    def test_a_missing_rc_is_never_created(self, tmp_path: Path) -> None:
        rc = tmp_path / ".zshrc"
        assert remove_alias_block(rc) is AliasRemoval.ABSENT
        assert not rc.exists()

    def test_writes_through_a_symlinked_rc_rather_than_replacing_it(self, tmp_path: Path) -> None:
        # A dotfiles-repo `~/.zshrc` is a symlink; replacing the link would detach
        # the operator's rc from the repo that manages it.
        real = tmp_path / "dotfiles" / "zshrc"
        real.parent.mkdir()
        real.write_text(_USER_RC + _MANAGED_ALIAS_BLOCK + _USER_RC_TAIL, encoding="utf-8")
        link = tmp_path / ".zshrc"
        link.symlink_to(real)

        assert remove_alias_block(link) is AliasRemoval.REMOVED
        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == _USER_RC + _USER_RC_TAIL

    def test_an_unreadable_rc_degrades_rather_than_raising(self, tmp_path: Path) -> None:
        rc = tmp_path / ".bashrc"
        rc.write_bytes(b"\xff\xfe not utf-8")
        assert remove_alias_block(rc) is AliasRemoval.UNWRITABLE


class TestLauncherPath:
    def test_defaults_to_local_bin_under_home(self, tmp_path: Path) -> None:
        assert launcher_bin_dir({"HOME": str(tmp_path)}) == tmp_path / ".local" / "bin"
        assert launcher_path({"HOME": str(tmp_path)}) == tmp_path / ".local" / "bin" / "t3"

    def test_uv_tool_bin_dir_wins(self, tmp_path: Path) -> None:
        env = {"HOME": str(tmp_path), "UV_TOOL_BIN_DIR": str(tmp_path / "custom")}
        assert launcher_bin_dir(env) == tmp_path / "custom"


class TestRenderLauncherScript:
    def test_execs_the_repo_wrapper_and_forwards_every_argument(self, tmp_path: Path) -> None:
        script = render_launcher_script(tmp_path)
        assert script.startswith("#!/usr/bin/env bash\n")
        assert LAUNCHER_MARKER in script
        assert f'exec "{wrapper_path(tmp_path)}" "$@"\n' in script

    def test_carries_no_cwd_resolution(self, tmp_path: Path) -> None:
        script = render_launcher_script(tmp_path)
        assert "$PWD" not in script
        assert "dirname" not in script
        assert "\ncd " not in script


class TestLauncherWrapperTarget:
    def test_reads_back_the_checkout_entry_a_launcher_execs(self, tmp_path: Path) -> None:
        assert launcher_wrapper_target(render_launcher_script(tmp_path)) == wrapper_path(tmp_path)

    def test_none_when_the_script_names_no_entry(self) -> None:
        assert launcher_wrapper_target(f"#!/usr/bin/env bash\n{LAUNCHER_MARKER}\n") is None


class TestInstallLauncher:
    def test_writes_an_executable_launcher(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        assert install_launcher(target, tmp_path / "clone") is LauncherInstall.INSTALLED
        assert target.read_text(encoding="utf-8") == render_launcher_script(tmp_path / "clone")
        assert target.stat().st_mode & 0o111

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        install_launcher(target, tmp_path / "clone")
        before = target.read_bytes()
        assert install_launcher(target, tmp_path / "clone") is LauncherInstall.ALREADY_PRESENT
        assert target.read_bytes() == before

    def test_repoints_a_managed_launcher_at_a_relocated_clone(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        install_launcher(target, tmp_path / "old-clone")
        assert install_launcher(target, tmp_path / "new-clone") is LauncherInstall.UPDATED
        assert launcher_wrapper_target(target.read_text(encoding="utf-8")) == wrapper_path(tmp_path / "new-clone")

    def test_replaces_the_uv_console_script_symlink(self, tmp_path: Path) -> None:
        uv_script = tmp_path / ".local" / "share" / "uv" / "tools" / "teatree" / "bin" / "t3"
        uv_script.parent.mkdir(parents=True)
        uv_script.write_text("#!/usr/bin/env python\n", encoding="utf-8")
        target = tmp_path / "bin" / "t3"
        target.parent.mkdir()
        target.symlink_to(uv_script)

        assert install_launcher(target, tmp_path / "clone") is LauncherInstall.UPDATED
        assert not target.is_symlink()
        assert is_managed_launcher(target)
        assert uv_script.is_file()

    def test_refuses_an_unmanaged_regular_file(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")

        assert install_launcher(target, tmp_path / "clone") is LauncherInstall.REFUSED
        assert target.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"

    def test_refuses_a_symlink_that_is_not_uvs(self, tmp_path: Path) -> None:
        elsewhere = tmp_path / "elsewhere" / "t3"
        elsewhere.parent.mkdir()
        elsewhere.write_text("#!/bin/sh\n", encoding="utf-8")
        target = tmp_path / "bin" / "t3"
        target.parent.mkdir()
        target.symlink_to(elsewhere)

        assert install_launcher(target, tmp_path / "clone") is LauncherInstall.REFUSED
        assert target.is_symlink()

    def test_unwritable_when_the_parent_is_a_file(self, tmp_path: Path) -> None:
        wall = tmp_path / "wall"
        wall.write_text("", encoding="utf-8")
        assert install_launcher(wall / "t3", tmp_path / "clone") is LauncherInstall.UNWRITABLE


class TestInstallIsAtomic:
    """``t3`` is the operator's ONLY entry point, so it must never blink out.

    The window a truncate-in-place (or an unlink-then-rewrite) opens is not
    theoretical: anything resolving ``t3`` during it — a git hook, cron, a
    sub-agent — gets "command not found" or "permission denied" from a machine
    that looks healthy a millisecond later. So the assertion is made from a
    concurrent resolver, which is the only vantage point that can see it.
    """

    def test_a_concurrent_resolver_always_finds_one_whole_executable_launcher(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        repos = [tmp_path / "clone-a", tmp_path / ("clone-" + "b" * 200)]
        whole = {render_launcher_script(repo) for repo in repos}
        install_launcher(target, repos[0])

        faults: list[str] = []
        stop = threading.Event()

        def resolve() -> None:
            while not stop.is_set():
                try:
                    if not os.access(target, os.X_OK):
                        faults.append(f"not executable: {target}")
                    elif target.read_text(encoding="utf-8") not in whole:
                        faults.append("torn read")
                except OSError as exc:
                    faults.append(f"unresolvable: {exc}")

        resolver = threading.Thread(target=resolve, daemon=True)
        resolver.start()
        start = threading.Barrier(len(repos))

        def install(repo: Path) -> None:
            start.wait()
            for _ in range(100):
                install_launcher(target, repo)

        writers = [threading.Thread(target=install, args=(repo,)) for repo in repos]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join()
        stop.set()
        resolver.join(timeout=5)

        assert faults == []
        # Last writer wins is fine; a mixture of the two scripts is not.
        assert read_managed_launcher(target) in whole
        assert os.access(target, os.X_OK)

    def test_a_failed_publish_leaves_the_previous_launcher_intact(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        install_launcher(target, tmp_path / "old-clone")
        previous = target.read_bytes()

        with patch.object(workflow.os, "replace", side_effect=OSError("interrupted")):
            assert install_launcher(target, tmp_path / "new-clone") is LauncherInstall.UNWRITABLE
        assert target.read_bytes() == previous
        assert os.access(target, os.X_OK)

    def test_leaves_no_scratch_file_beside_the_launcher(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        install_launcher(target, tmp_path / "old-clone")
        install_launcher(target, tmp_path / "new-clone")
        with patch.object(workflow.os, "replace", side_effect=OSError("interrupted")):
            install_launcher(target, tmp_path / "third-clone")
        assert [entry.name for entry in target.parent.iterdir()] == ["t3"]


class TestInstallIsVerified:
    """Success is read back from the published file, never assumed from the write."""

    def test_a_publish_that_lands_nothing_is_unverified(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        with patch.object(workflow.os, "replace", lambda *_args: None):
            assert install_launcher(target, tmp_path / "clone") is LauncherInstall.UNVERIFIED
        assert not target.exists()

    def test_a_publish_that_lands_another_checkout_is_unverified(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        decoy = render_launcher_script(tmp_path / "somewhere-else")

        def land_the_decoy(_staged: Path, destination: Path) -> None:
            destination.write_text(decoy, encoding="utf-8")
            destination.chmod(0o755)

        with patch.object(workflow.os, "replace", land_the_decoy):
            assert install_launcher(target, tmp_path / "clone") is LauncherInstall.UNVERIFIED

    def test_a_non_executable_publish_is_unverified(self, tmp_path: Path) -> None:
        target = tmp_path / "bin" / "t3"
        script = render_launcher_script(tmp_path / "clone")

        def land_without_the_exec_bit(_staged: Path, destination: Path) -> None:
            destination.write_text(script, encoding="utf-8")
            destination.chmod(0o644)

        with patch.object(workflow.os, "replace", land_without_the_exec_bit):
            assert install_launcher(target, tmp_path / "clone") is LauncherInstall.UNVERIFIED
