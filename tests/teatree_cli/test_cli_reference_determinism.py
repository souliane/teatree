"""The CLI reference render must be byte-identical across environments.

souliane/teatree#2599: the ``docs-drift`` gate could not catch drift in
``docs/generated/cli-reference.md`` because (1) the generator auto-staged its own
output (masking ``git diff`` with no ``--cached``) and (2) the render embedded the
terminal width and the resolving home directory, so a local regen and a CI regen
produced different bytes. These pin the deterministic render so the un-masked gate
catches genuine drift instead of flapping on non-reproducible byte diffs.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from teatree.cli import app as _t3_app
from teatree.cli import register_overlay_commands
from teatree.cli.command_tree import render_cli_reference_deterministic

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATOR = _REPO_ROOT / "scripts" / "hooks" / "generate_cli_reference.py"


@pytest.fixture
def real_app() -> typer.Typer:
    register_overlay_commands(allowlist={"t3-teatree"})
    return _t3_app


class TestRenderIsWidthIndependent:
    def test_identical_across_columns_values(self, real_app: typer.Typer, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COLUMNS", "80")
        narrow = render_cli_reference_deterministic(real_app)
        monkeypatch.setenv("COLUMNS", "200")
        wide = render_cli_reference_deterministic(real_app)
        assert narrow == wide

    def test_render_restores_columns_env(self, real_app: typer.Typer, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COLUMNS", "123")
        render_cli_reference_deterministic(real_app)
        assert os.environ.get("COLUMNS") == "123"


class TestRenderIsHomePathIndependent:
    _ABS_CONFIG = re.compile(r"(?:/[^/\s│\]]+)+/\.teatree\.toml")

    def test_no_absolute_home_rooted_config_path_in_output(self, real_app: typer.Typer) -> None:
        markdown = render_cli_reference_deterministic(real_app)
        hits = self._ABS_CONFIG.findall(markdown)
        assert not hits, f"absolute home-rooted config path(s) leaked into the render: {hits}"

    def test_home_path_normalized_to_tilde(self, real_app: typer.Typer) -> None:
        markdown = render_cli_reference_deterministic(real_app)
        assert "~/.teatree.toml" in markdown


class TestGeneratorRenderIsByteStableAcrossEnvironments:
    """End-to-end: invoking the generator with different env produces identical bytes."""

    def _generate(self, out: Path, env_overrides: dict[str, str]) -> str:
        env = {**os.environ, **env_overrides, "DJANGO_SETTINGS_MODULE": "teatree.settings"}
        subprocess.run(
            [sys.executable, str(_GENERATOR), str(out)],
            check=True,
            cwd=_REPO_ROOT,
            env=env,
        )
        return out.read_text(encoding="utf-8")

    def test_byte_identical_across_columns_and_home(self, tmp_path: Path) -> None:
        a = self._generate(tmp_path / "a.md", {"COLUMNS": "80", "HOME": "/tmp/fake-home-a"})
        b = self._generate(tmp_path / "b.md", {"COLUMNS": "200", "HOME": "/tmp/fake-home-b"})
        assert a == b
