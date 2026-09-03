"""The seam every hook invokes ``t3`` through — the cwd guarantee and its postures.

The behaviour half of the guard; the static half is
``tests/conformance/test_hook_t3_invocation_seam.py``, which fails when a hook
resolves or spawns ``t3`` without coming through here. Both are needed: the
conformance lane proves every call site ARRIVES at the seam, and these tests prove
arriving is worth something.

The property under test is one sentence: a hook subprocess must not inherit the
harness session directory, because the containerized ``t3`` refuses a directory it
cannot see from inside the container and a gate that fails CLOSED turns that
refusal into a DENY on correct work.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from hooks.scripts import t3_invocation


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["t3"], returncode=0, stdout="", stderr="")


class TestCheckoutRoot:
    def test_it_resolves_the_checkout_holding_this_hook_package(self) -> None:
        root = t3_invocation.hook_checkout_root()
        assert root is not None
        assert (root / "hooks" / "scripts" / "t3_invocation.py").is_file()

    def test_the_invocation_cwd_is_that_root(self) -> None:
        assert t3_invocation.t3_invocation_cwd() == str(t3_invocation.hook_checkout_root())

    def test_a_broken_layout_is_announced_not_silent(self, tmp_path, capsys) -> None:
        """Fail LOUD, never closed: the call still proceeds, but the degradation is named."""
        with patch.object(t3_invocation, "__file__", str(tmp_path / "stray.py")):
            assert t3_invocation.hook_checkout_root() is None
            assert t3_invocation.t3_invocation_cwd() is None
        assert "could not locate its own checkout" in capsys.readouterr().err


class TestArgvResolution:
    def test_it_builds_the_argv_from_the_resolved_binary(self) -> None:
        with patch.object(t3_invocation.shutil, "which", return_value="/usr/local/bin/t3"):
            assert t3_invocation.t3_argv("tool", "validate-mr") == ["/usr/local/bin/t3", "tool", "validate-mr"]
            assert t3_invocation.t3_available() is True

    def test_an_absent_binary_yields_no_argv(self) -> None:
        with patch.object(t3_invocation.shutil, "which", return_value=None):
            assert t3_invocation.t3_argv("tool", "validate-mr") is None
            assert t3_invocation.t3_available() is False


class TestRunPinsTheCwd:
    def test_it_pins_the_checkout_root_by_default(self) -> None:
        with patch("subprocess.run", return_value=_completed()) as run:
            t3_invocation.run_t3(["t3", "tool", "validate-mr"], timeout=9)
        assert run.call_args.kwargs["cwd"] == str(t3_invocation.hook_checkout_root())

    def test_it_never_inherits_the_callers_directory(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        with patch("subprocess.run", return_value=_completed()) as run:
            t3_invocation.run_t3(["t3", "loop", "pending-spawn"], timeout=9)
        assert Path(run.call_args.kwargs["cwd"]).resolve() != tmp_path.resolve()

    def test_an_explicit_cwd_wins(self, tmp_path) -> None:
        with patch("subprocess.run", return_value=_completed()) as run:
            t3_invocation.run_t3(["t3", "tool", "diff-coverage"], timeout=9, cwd=tmp_path)
        assert run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_it_fixes_the_house_posture_so_call_sites_cannot_drift(self) -> None:
        with patch("subprocess.run", return_value=_completed()) as run:
            t3_invocation.run_t3(["t3", "tool", "ai-sig-scan", "-"], timeout=9, stdin_text="body")
        kwargs = run.call_args.kwargs
        assert (kwargs["capture_output"], kwargs["text"], kwargs["check"]) == (True, True, False)
        assert (kwargs["timeout"], kwargs["input"]) == (9, "body")


class TestDetachedSpawnPinsTheCwd:
    def test_it_pins_the_checkout_root(self) -> None:
        with patch("subprocess.Popen") as popen:
            t3_invocation.spawn_t3_detached(["t3", "speak", "hello"])
        assert popen.call_args.kwargs["cwd"] == str(t3_invocation.hook_checkout_root())

    def test_it_detaches_so_a_slow_call_never_holds_the_hook_open(self) -> None:
        with patch("subprocess.Popen") as popen:
            t3_invocation.spawn_t3_detached(["t3", "speak-dm", "--text", "hi"])
        kwargs = popen.call_args.kwargs
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
