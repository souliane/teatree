"""``managepy`` must not drive an overlay project whose venv is from the other side.

``deploy/t3`` bind-mounts the operator's working tree into the container, so the
overlay project the container resolves carries the HOST's ``.venv``. Routing a
bridged subcommand through ``uv --directory <project> run`` there makes uv REMOVE
that environment — a ``t3 <overlay> tasks list`` destroying the tree the operator is
working in. The overlay is importable in the running interpreter either way, so the
``python -m teatree`` path is taken instead.

Real ``manage.py`` + ``pyvenv.cfg`` under ``tmp_path``; only ``run_streamed`` (the
subprocess boundary) is mocked, so the assertion is on the argv actually emitted.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder


def _overlay_project(tmp_path: Path, *, venv_home: Path | str | None) -> Path:
    project = tmp_path / "fork"
    project.mkdir()
    (project / "manage.py").write_text('os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fork.settings")\n')
    if venv_home is not None:
        (project / ".venv").mkdir()
        (project / ".venv" / "pyvenv.cfg").write_text(f"home = {venv_home}\n", encoding="utf-8")
    return project


def _build_app(project: Path) -> typer.Typer:
    return OverlayAppBuilder(overlay_name="t3-fork", project_path=project, settings_module="fork.settings").build()


def _emitted_argv(project: Path) -> list[str]:
    with patch("teatree.cli.overlay.run_streamed") as run_streamed:
        result = CliRunner().invoke(_build_app(project), ["tasks", "list"])
    assert result.exit_code == 0, result.output
    return list(run_streamed.call_args.args[0])


def test_foreign_platform_venv_is_not_driven_through_uv_run(tmp_path: Path) -> None:
    argv = _emitted_argv(_overlay_project(tmp_path, venv_home="/absent-on-this-side/uv/python/cpython-3.13/bin"))

    assert "uv" not in argv, f"uv run would delete the host's .venv, got {argv!r}"
    assert "manage.py" not in " ".join(argv), f"expected the python -m teatree path, got {argv!r}"
    assert argv[1:3] == ["-m", "teatree"], argv


@pytest.mark.parametrize("venv_home_subdir", [None, "pythons/cpython-3.13/bin"])
def test_native_project_still_routes_through_its_own_manage_py(
    tmp_path: Path,
    venv_home_subdir: str | None,
) -> None:
    """The host and the deployment box are unchanged — with or without a venv present."""
    venv_home = tmp_path / venv_home_subdir if venv_home_subdir else None
    if venv_home is not None:
        venv_home.mkdir(parents=True)

    project = _overlay_project(tmp_path, venv_home=venv_home)

    argv = _emitted_argv(project)

    assert "manage.py" in argv, argv
    assert str(project) in argv, argv
