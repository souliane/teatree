"""The destructive reinstall refuses without free-space headroom, and DEFERS rather than fails (#4338).

``uv tool install … --reinstall`` deletes the working tool venv before rebuilding it. With
391 MB free it left 124 packages, no ``click``, and every ``t3`` invocation dead at
``import typer`` — the CLI unusable in every container and the worker crash-looping until
the disk was reclaimed by hand. A partial result is strictly worse than not having run.

Both directions are driven against the REAL filesystem measurement: the floor is moved by
``TEATREE_INSTALL_MIN_FREE_MB`` rather than by mocking the disk, so a guard that refuses
unconditionally and a guard wired to a floor of zero each fail one half. The load-bearing
assertion is the negative one — with no headroom the ``--reinstall`` argv is never handed
to the runner at all, which is what "leaves the previous venv intact" means.
"""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import django.test
import pytest

import teatree.self_update as self_update_mod
from teatree.core.models.pending_reinstall import PendingReinstall
from teatree.loop.self_update_reinstall import DrainOutcome, drain_pending_reinstall
from teatree.self_update import ReinstallResult, reinstall_running_editable
from teatree.utils.install_headroom import DEFAULT_INSTALL_MIN_FREE_MB, INSTALL_MIN_FREE_MB_ENV

_IMPOSSIBLE_FLOOR_MB = "999999999"
_NO_FLOOR_MB = "0"


@dataclass
class _Proc:
    returncode: int
    stdout: str
    stderr: str


def _which_all(name: str) -> str:
    return f"/usr/bin/{name}"


class _RecordingRunner:
    def __init__(self, tool_dir: Path) -> None:
        self.tool_dir = tool_dir
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: object) -> _Proc:
        self.calls.append(cmd)
        if cmd[1:3] == ["tool", "dir"]:
            return _Proc(0, str(self.tool_dir), "")
        return _Proc(0, "ok", "")

    @property
    def reinstalled(self) -> bool:
        return any("--reinstall" in cmd for cmd in self.calls)

    @property
    def ran_setup(self) -> bool:
        return any(cmd[-1] == "setup" for cmd in self.calls)


@pytest.fixture
def editable_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RecordingRunner:
    source = tmp_path / "editable-src"
    source.mkdir()
    tool_dir = tmp_path / "uv-tools"
    tool_dir.mkdir()
    monkeypatch.setattr(self_update_mod.shutil, "which", _which_all)
    monkeypatch.setattr(self_update_mod, "current_editable_source", lambda _uv: source)
    return _RecordingRunner(tool_dir)


def test_no_headroom_refuses_before_the_destructive_argv_runs(
    editable_install: _RecordingRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(INSTALL_MIN_FREE_MB_ENV, _IMPOSSIBLE_FLOOR_MB)

    result = reinstall_running_editable(runner=editable_install)

    assert result == ReinstallResult(ok=False, reinstalled=False, error=result.error, deferred=True)
    assert not editable_install.reinstalled, "the previous venv was destroyed despite the refusal"
    assert not editable_install.ran_setup, "setup is not the problem; a refusal must not burn a minute on it"
    assert "MB free" in result.error


def test_ample_headroom_still_runs_the_reinstall(
    editable_install: _RecordingRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anti-vacuity: the guard must gate the install, not disable it."""
    monkeypatch.setenv(INSTALL_MIN_FREE_MB_ENV, _NO_FLOOR_MB)

    result = reinstall_running_editable(runner=editable_install)

    assert result.ok is True
    assert result.deferred is False
    assert editable_install.reinstalled


def test_an_unresolvable_tool_dir_proceeds_rather_than_locking_self_update_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent reading is not evidence of no room — refusing on it would be a lockout."""
    source = tmp_path / "editable-src"
    source.mkdir()
    monkeypatch.setattr(self_update_mod.shutil, "which", _which_all)
    monkeypatch.setattr(self_update_mod, "current_editable_source", lambda _uv: source)
    monkeypatch.setenv(INSTALL_MIN_FREE_MB_ENV, _IMPOSSIBLE_FLOOR_MB)
    calls: list[list[str]] = []

    def _runner(cmd: list[str], **_kwargs: object) -> _Proc:
        calls.append(cmd)
        return _Proc(1, "", "no such command") if cmd[1:3] == ["tool", "dir"] else _Proc(0, "ok", "")

    assert reinstall_running_editable(runner=_runner).deferred is False
    assert any("--reinstall" in cmd for cmd in calls)


def test_the_default_floor_is_two_gigabytes() -> None:
    assert DEFAULT_INSTALL_MIN_FREE_MB == 2048


class TestDeferredReinstallLeavesTheRowPending(django.test.TestCase):
    def test_drain_defers_and_does_not_mark_the_row_failed(self) -> None:
        """One transient low-disk moment must not permanently disarm self-update."""
        row = PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="deadbeef")
        refused = ReinstallResult(ok=False, reinstalled=False, error="no headroom", deferred=True)

        with patch.object(self_update_mod, "reinstall_running_editable", return_value=refused):
            result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.DEFERRED
        row.refresh_from_db()
        assert row.state == PendingReinstall.State.PENDING

    def test_a_genuine_failure_still_marks_the_row_failed(self) -> None:
        """Anti-vacuity: only a REFUSAL defers — a real reinstall failure must still be recorded."""
        row = PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="deadbeef")
        failed = ReinstallResult(ok=False, reinstalled=False, error="uv exploded")

        with patch.object(self_update_mod, "reinstall_running_editable", return_value=failed):
            result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.FAILED
        row.refresh_from_db()
        assert row.state == PendingReinstall.State.FAILED
