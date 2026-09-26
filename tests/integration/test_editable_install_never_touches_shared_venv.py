"""An agent's editable install of its checkout can never land in the dispatcher's shared venv.

Real ``uv``, real venvs. The dispatcher runs under ``VIRTUAL_ENV=<shared venv>``; the agent
editable-installs the checkout it works in. Inside the spawn window that install must go to
the checkout's own venv, so reaping the checkout later leaves the shared venv importable.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.agents._runner_env import agent_spawn_env
from teatree.utils.editable_pth import pth_source_dirs

pytestmark = pytest.mark.integration


def _uv() -> str:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available")
    return uv


def _venv(uv: str, path: Path) -> Path:
    subprocess.run([uv, "venv", "--quiet", "--python", sys.executable, str(path)], check=True, capture_output=True)
    return next(path.glob("lib/python*/site-packages"))


def _checkout(root: Path) -> Path:
    package = root / "src" / "wt_probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "wt-probe"\nversion = "0.0.0"\n\n'
        '[build-system]\nrequires = ["uv_build>=0.8,<1"]\nbuild-backend = "uv_build"\n',
        encoding="utf-8",
    )
    return root


def test_an_editable_install_inside_the_spawn_window_leaves_the_shared_venv_intact(tmp_path: Path) -> None:
    uv = _uv()
    shared = tmp_path / "shared"
    shared_site = _venv(uv, shared)
    (shared_site / "shared_probe.py").write_text("VALUE = 1\n", encoding="utf-8")
    checkout = _checkout(tmp_path / "checkout")
    _venv(uv, checkout / ".venv")

    with patch.dict(os.environ, {"VIRTUAL_ENV": str(shared)}), agent_spawn_env():
        subprocess.run(
            [uv, "pip", "install", "--quiet", "-e", str(checkout)],
            cwd=checkout,
            env=dict(os.environ),
            check=True,
            capture_output=True,
        )

    shared_lines = [line for pth in shared_site.glob("*.pth") for line in pth_source_dirs(pth)]
    assert not [line for line in shared_lines if line.is_relative_to(checkout)]

    shutil.rmtree(checkout)
    imported = subprocess.run(
        [str(shared / "bin" / "python"), "-c", "import shared_probe"], capture_output=True, text=True, check=False
    )
    assert imported.returncode == 0, imported.stderr
    assert all(line.exists() for line in shared_lines)
