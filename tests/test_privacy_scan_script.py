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
        result = _run("token = glpat-XXXXXXXXXXXXXXXX\n")  # privacy-scan:allow self-fixture
        assert result.returncode == 1, result.stdout + result.stderr

    def test_internal_home_path_exits_nonzero(self) -> None:
        result = _run("see /Users/someone/secret/path\n")  # privacy-scan:allow self-fixture
        assert result.returncode == 1, result.stdout + result.stderr

    def test_clean_text_exits_zero(self) -> None:
        result = _run("a perfectly ordinary line of prose\n")
        assert result.returncode == 0, result.stdout + result.stderr


class TestPrivacyScanAllowAnnotation:
    """A line carrying the inline ``privacy-scan:allow`` annotation is exempt.

    Same idiom as gitleaks' ``gitleaks:allow``. Used so a repo's own
    privacy-scanner fixtures and the gate's own documentation examples do
    not self-block the gate, while a real leak on any line *without* the
    annotation is still caught.
    """

    def test_annotated_line_is_exempt(self) -> None:
        result = _run("token = glpat-XXXXXXXXXXXXXXXX  # privacy-scan:allow planted fixture\n")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_annotated_line_does_not_exempt_other_lines(self) -> None:
        text = (
            "token = glpat-XXXXXXXXXXXXXXXX  # privacy-scan:allow fixture\n"
            "real = glpat-YYYYYYYYYYYYYYYY\n"  # privacy-scan:allow self-fixture
        )
        result = _run(text)
        assert result.returncode == 1, result.stdout + result.stderr

    def test_annotation_only_exempts_its_own_line_not_a_substring_match(self) -> None:
        # The annotation must be the literal marker, not any line that
        # merely mentions the word "allow".
        result = _run("token = glpat-XXXXXXXXXXXXXXXX  # allow this please\n")  # privacy-scan:allow self-fixture
        assert result.returncode == 1, result.stdout + result.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
