"""``_check_interpreter_plane`` — a venue-invalid environment is LOUD (#4642).

The shared interpreter root makes a venue-triggered rebuild structurally
impossible; this probe makes any RESIDUAL mismatch reportable instead of costing
a silent gigabyte. Both halves are covered: an interpreter root that cannot
supply the project's Python, and a checkout whose recorded ``home`` names a root
this venue does not have.
"""

import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import teatree.config as config_mod
from teatree.cli.doctor.checks_interpreter_plane import _check_interpreter_plane, _required_python_floor

FLOOR = "{}.{}".format(*_required_python_floor())


def _run(interpreter_root: Path, worktree_root: Path) -> tuple[bool, str]:
    out = io.StringIO()
    with (
        patch.dict("os.environ", {"UV_PYTHON_INSTALL_DIR": str(interpreter_root)}),
        patch.object(config_mod, "worktree_root", return_value=worktree_root),
        redirect_stdout(out),
    ):
        ok = _check_interpreter_plane()
    return ok, out.getvalue()


def _build_interpreter_root(tmp_path: Path, version: str = FLOOR) -> Path:
    root = tmp_path / "uv" / "python"
    (root / f"cpython-{version}.9-linux-x86_64-gnu" / "bin").mkdir(parents=True)
    return root


def _build_checkout(worktree_root: Path, recorded_home: Path) -> Path:
    checkout = worktree_root / "1234-a-ticket" / "teatree"
    venv = checkout / ".venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = {recorded_home}\nversion = {FLOOR}.9\n", encoding="utf-8")
    return checkout


class TestCheckInterpreterPlane:
    def test_passes_when_every_recorded_home_resolves_in_this_venue(self, tmp_path: Path) -> None:
        root = _build_interpreter_root(tmp_path)
        worktree_root = tmp_path / "t3-workspaces"
        _build_checkout(worktree_root, next(root.iterdir()) / "bin")

        ok, message = _run(root, worktree_root)

        assert ok is True
        assert "FAIL" not in message

    def test_fails_when_a_checkout_records_a_root_this_venue_does_not_have(self, tmp_path: Path) -> None:
        root = _build_interpreter_root(tmp_path)
        worktree_root = tmp_path / "t3-workspaces"
        # Under tmp_path on purpose: the OTHER venue's real root exists in this
        # one whenever the suite runs there, so a literal would prove nothing.
        foreign = tmp_path / "other-venue" / "uv" / "python" / f"cpython-{FLOOR}-linux-x86_64-gnu" / "bin"
        checkout = _build_checkout(worktree_root, foreign)

        ok, message = _run(root, worktree_root)

        assert ok is False
        assert "FAIL" in message
        assert str(foreign) in message, "the message must name the root the environment recorded"
        assert str(root) in message, "the message must name this venue's own root"
        assert str(checkout) in message, "the message must name the checkout that would be rebuilt"

    def test_fails_when_this_venues_root_cannot_supply_the_projects_python(self, tmp_path: Path) -> None:
        empty_root = tmp_path / "uv" / "python"
        empty_root.mkdir(parents=True)
        worktree_root = tmp_path / "t3-workspaces"
        worktree_root.mkdir()

        ok, message = _run(empty_root, worktree_root)

        assert ok is False
        assert "FAIL" in message
        assert str(empty_root) in message
        assert FLOOR in message, "the message must name the version that is missing"
