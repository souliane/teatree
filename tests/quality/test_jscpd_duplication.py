"""Conformance ledger for the jscpd duplication gate (layer 4 anti-drift).

Two invariants.

Config pin: ``.jscpd.json`` keeps the design thresholds (min-lines 6,
min-tokens 40, threshold 1.5 = the blocking duplication ratchet that locks in
the dedup-burst cleanup — only shrinks) and a high max-lines/max-size so jscpd
never silently skips a large source file. A loosening turns this red.

Scan coverage: every ``src/teatree/**/*.py`` file large enough to contain a
``minLines``-line clone (>= ``minLines`` physical lines, migrations excluded) is
in jscpd's analyzed set. A file below ``minLines`` cannot hold a clone of that
size, so jscpd legitimately omits it. This is the openclaw scan-coverage
pattern: no clone-capable source escapes the scanner.

jscpd's default ``max-lines`` (1000) and ``max-size`` (100kb) silently drop a
large file — the bug this assertion pins. Skipped (conditional) when node/npx is
absent, the same shape as the env-dependent skips elsewhere in the suite.
"""

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

# The whole-tree jscpd scan is the ~63s cost that made every push time out; it is
# deselected at push (`-m "not push_heavy"`) and runs in CI instead.
pytestmark = pytest.mark.push_heavy

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _REPO_ROOT / ".jscpd.json"
_SRC = _REPO_ROOT / "src" / "teatree"
_NPX = shutil.which("npx") or "npx"


def _line_count(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("\n") + 1


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads(_CONFIG.read_text(encoding="utf-8"))


class TestConfigPin:
    def test_thresholds_match_design(self, config: dict) -> None:
        assert config["minLines"] == 6
        assert config["minTokens"] == 40
        assert math.isclose(config["threshold"], 1.5)

    def test_python_format_only(self, config: dict) -> None:
        assert config["format"] == ["python"]

    def test_max_lines_and_size_prevent_silent_skip(self, config: dict) -> None:
        biggest = max(_line_count(p) for p in _SRC.rglob("*.py"))
        assert int(config["maxLines"]) > biggest
        assert config["maxSize"].endswith(("mb", "MB"))


def _expected_clone_capable_files(min_lines: int) -> set[Path]:
    return {p.resolve() for p in _SRC.rglob("*.py") if "migrations" not in p.parts and _line_count(p) >= min_lines}


# Whole-tree jscpd scan is ~60s standalone and stretches further under
# concurrent-coder load; the 60s default pytest-timeout deterministically
# tripped and blocked every push through the ci-critical-parity hook.
@pytest.mark.timeout(300)
@pytest.mark.integration
@pytest.mark.skipif(shutil.which("npx") is None, reason="npx (node) not on PATH")
class TestScanCoverage:
    @pytest.fixture(scope="class")
    def analyzed(self, tmp_path_factory: pytest.TempPathFactory) -> set[Path]:
        out = tmp_path_factory.mktemp("jscpd")
        subprocess.run(
            [
                _NPX,
                "--yes",
                "jscpd@4",
                "--config",
                str(_CONFIG),
                "--reporters",
                "json",
                "--output",
                str(out),
                "--silent",
                str(_SRC),
            ],
            cwd=_REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        report = json.loads((out / "jscpd-report.json").read_text(encoding="utf-8"))
        return {Path(p).resolve() for p in report["statistics"]["formats"]["python"]["sources"]}

    def test_no_clone_capable_file_escapes(self, analyzed: set[Path], config: dict) -> None:
        expected = _expected_clone_capable_files(int(config["minLines"]))
        escaped = sorted(p.relative_to(_REPO_ROOT).as_posix() for p in expected - analyzed)
        assert not escaped, f"source files not scanned by jscpd: {escaped}"

    def test_tree_has_no_duplication(self, tmp_path: Path) -> None:
        # Its own ``--output`` dir so two concurrent jscpd runs (this and the
        # ``analyzed`` fixture, under ``-n auto``) never share an artifact dir.
        result = subprocess.run(
            [_NPX, "--yes", "jscpd@4", "--config", str(_CONFIG), "--output", str(tmp_path), "--silent", str(_SRC)],
            cwd=_REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
