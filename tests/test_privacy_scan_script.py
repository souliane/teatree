"""Integration tests for ``scripts/privacy_scan.py`` as a subprocess.

``t3 tool privacy-scan`` runs this script via
``ToolRunner.run_script`` → ``[sys.executable, script, *args]``. Without
an ``if __name__ == "__main__"`` guard the typer ``app`` is never
invoked and the script is a silent no-op (exit 0 on a planted secret),
which makes the retro/contribute privacy scan worthless. These tests
invoke the script the same way ``run_script`` does so the entrypoint is
exercised, not mocked.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "privacy_scan.py"


def _run(stdin: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "-"],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


class TestPrivacyScanScriptEntrypoint:
    def test_planted_api_key_exits_nonzero(self) -> None:
        result = _run("token = glpat-XXXXXXXXXXXXXXXX\n")
        assert result.returncode == 1, result.stdout + result.stderr

    def test_internal_home_path_exits_nonzero(self) -> None:
        result = _run("see /Users/someone/secret/path\n")
        assert result.returncode == 1, result.stdout + result.stderr

    def test_clean_text_exits_zero(self) -> None:
        result = _run("a perfectly ordinary line of prose\n")
        assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
