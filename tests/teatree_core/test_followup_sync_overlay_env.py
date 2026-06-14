"""``followup sync`` for a non-default overlay must run in that overlay's env (#2221).

``t3 <overlay> followup sync`` is core-dispatched: it shells out to
``python -m teatree followup sync`` with ``T3_OVERLAY_NAME`` pinned. The bug:
the subprocess ran under ``sys.executable`` — the uv-tool teatree install —
whose ``teatree.overlays`` entry-point registry only contains the *default*
overlay. So ``get_overlay(<secondary>)`` inside the subprocess raised
``ImproperlyConfigured: Overlay '<secondary>' not found`` and the reconcile
never ran, leaving the secondary overlay's task DB permanently un-synced
against the forge.

The fix routes core dispatch through the *named overlay's* project
environment (``runner_prefix(project_path)`` → ``uv --directory <path> run
python``) when that overlay has its own project dir, so the subprocess
interpreter has the secondary overlay's package importable and
``get_overlay`` resolves it. An entry-point overlay with no project dir is
installed in the same env that runs ``t3``, so it keeps using
``sys.executable``.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder


def _secondary_overlay_clone(tmp_path: Path) -> Path:
    """A secondary overlay's own clone, with a ``manage.py`` marking a project env."""
    clone = tmp_path / "t3-secondary"
    clone.mkdir()
    (clone / "manage.py").write_text("# stub secondary overlay manage.py\n", encoding="utf-8")
    return clone


def _build_secondary_app(project_path: Path) -> typer.Typer:
    return OverlayAppBuilder(overlay_name="secondary", project_path=project_path).build()


def test_followup_sync_runs_in_secondary_overlay_env(tmp_path: Path) -> None:
    """The subprocess runs from the secondary overlay's project env, not bare ``sys.executable``.

    Anti-vacuity: the pre-fix dispatch is ``[sys.executable, '-m', 'teatree',
    ...]`` — it never references the overlay's project dir, so the subprocess
    interpreter cannot import the secondary overlay and ``get_overlay`` raises
    ``Overlay not found``. This asserts the project dir reaches the command,
    which is exactly what the pre-fix path omits.
    """
    clone = _secondary_overlay_clone(tmp_path)
    app = _build_secondary_app(clone)
    with (
        patch("teatree.cli.overlay._overlay_project_env", return_value=clone),
        patch("teatree.cli.overlay.run_streamed") as run_streamed,
    ):
        result = CliRunner().invoke(app, ["followup", "sync"])
    assert result.exit_code == 0, result.output
    cmd = run_streamed.call_args.args[0]
    assert str(clone) in cmd, f"core dispatch must run in the secondary overlay's env, got {cmd!r}"
    assert cmd[0] != sys.executable, f"must not run under the teatree-install interpreter, got {cmd!r}"
    assert "-m" in cmd, f"still a `python -m teatree` invocation, got {cmd!r}"
    assert "teatree" in cmd, f"still a `python -m teatree` invocation, got {cmd!r}"
    assert "manage.py" not in " ".join(cmd), f"core dispatch never routes through manage.py, got {cmd!r}"
    assert "followup" in cmd, f"followup subcommand must reach the subprocess, got {cmd!r}"
    assert "sync" in cmd, f"sync subcommand must reach the subprocess, got {cmd!r}"


def test_followup_sync_passes_overlay_name_to_subprocess(tmp_path: Path) -> None:
    """``T3_OVERLAY_NAME`` is pinned so the subprocess ``get_overlay`` resolves the right one."""
    clone = _secondary_overlay_clone(tmp_path)
    app = _build_secondary_app(clone)
    with (
        patch("teatree.cli.overlay._overlay_project_env", return_value=clone),
        patch("teatree.cli.overlay.run_streamed") as run_streamed,
    ):
        result = CliRunner().invoke(app, ["followup", "sync"])
    assert result.exit_code == 0, result.output
    env = run_streamed.call_args.kwargs["env"]
    assert env["T3_OVERLAY_NAME"] == "secondary"


def test_followup_sync_falls_back_to_sys_executable_without_project_env(tmp_path: Path) -> None:
    """An entry-point overlay with no project dir keeps the ``sys.executable`` path.

    Such an overlay is installed in the same env that runs ``t3``, so its
    package is already importable in ``sys.executable`` and no project-env
    redirect is needed. The fix must not break this single-overlay default.
    """
    clone = _secondary_overlay_clone(tmp_path)
    app = _build_secondary_app(clone)
    with (
        patch("teatree.cli.overlay._overlay_project_env", return_value=None),
        patch("teatree.cli.overlay.run_streamed") as run_streamed,
    ):
        result = CliRunner().invoke(app, ["followup", "sync"])
    assert result.exit_code == 0, result.output
    cmd = run_streamed.call_args.args[0]
    assert cmd[0] == sys.executable, f"no project env -> run under sys.executable, got {cmd!r}"
    assert cmd[1:3] == ["-m", "teatree"], f"still `python -m teatree`, got {cmd!r}"
