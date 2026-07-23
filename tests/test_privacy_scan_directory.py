"""Directory-argument handling for ``scripts/privacy_scan.py`` (#3675 defect 1).

``t3 tool privacy-scan .`` is the scanner's most obvious invocation, yet a
directory argument previously raised an unhandled ``IsADirectoryError`` from
``Path.read_text``. A leak scanner must not traceback on that call: given a
directory it walks the tree, scanning each text file and naming the file so a
finding is locatable.
"""

import subprocess
import sys
from pathlib import Path

from scripts.privacy_scan import PRIVACY_FINDINGS_EXIT_CODE

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "privacy_scan.py"

# Planted fixture value the scanner must flag; annotated so this repo's own
# pre-push gate does not flag the test source itself.
_PLANTED_SECRET = "token = glpat-XXXXXXXXXXXXXXXX\n"  # privacy-scan:allow fixture


def _scan(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestPrivacyScanDirectoryArgument:
    def test_directory_with_a_leak_exits_findings_code_without_traceback(self, tmp_path: Path) -> None:
        (tmp_path / "config.txt").write_text(_PLANTED_SECRET, encoding="utf-8")

        result = _scan(tmp_path)

        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "IsADirectoryError" not in result.stderr, result.stderr
        assert "Traceback" not in result.stderr, result.stderr

    def test_directory_finding_names_the_file_it_is_in(self, tmp_path: Path) -> None:
        nested = tmp_path / "pkg"
        nested.mkdir()
        (nested / "secrets.txt").write_text(_PLANTED_SECRET, encoding="utf-8")

        result = _scan(tmp_path)

        assert result.returncode == PRIVACY_FINDINGS_EXIT_CODE, result.stdout + result.stderr
        assert "pkg/secrets.txt" in result.stdout, result.stdout
        assert "api_key" in result.stdout, result.stdout

    def test_clean_directory_exits_zero(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("a perfectly ordinary line of prose\n", encoding="utf-8")

        result = _scan(tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "clean" in result.stdout.lower()

    def test_binary_and_vcs_files_are_skipped_not_crashed_on(self, tmp_path: Path) -> None:
        (tmp_path / "image.bin").write_bytes(b"\x00\x01\x02\xff\xfe not utf-8 \x80")
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(_PLANTED_SECRET, encoding="utf-8")  # inside .git — must be skipped
        (tmp_path / "clean.txt").write_text("all good here\n", encoding="utf-8")

        result = _scan(tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "Traceback" not in result.stderr, result.stderr
