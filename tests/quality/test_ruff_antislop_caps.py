"""Conformance ledger for the ruff anti-slop caps (layer 4 anti-drift).

Three AST ceilings that must stay merge-blocking under the project ruff config:

- **C901** — cyclomatic-complexity ceiling, pinned explicit so it cannot drift.
- **FIX** (flake8-fixme) — bans TODO/FIXME/XXX/HACK presence in a comment.
- **ERA** (eradicate) — bans commented-out code.

(TD enforces TODO *formatting*; FIX is the ban on the marker's presence.)

Each test plants a violating probe and asserts ruff reports it under the real
project config (the cap *bites*), plus an enablement guard that turns red if a
future PR adds the rule to ``lint.ignore``. The tree itself is already green on
all three (verified by the project-wide ``ruff check`` in CI).
"""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

_CODE_RE = re.compile(r"\b([A-Z]+\d+)\b")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_UV = shutil.which("uv") or "uv"


def _load_lint() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]


@pytest.fixture(scope="module")
def ruff_lint() -> dict:
    return _load_lint()


def _ignored_codes(lint: dict) -> set[str]:
    return set(lint.get("ignore", [])) | set(lint.get("extend-ignore", []))


def _ruff_codes(target: Path) -> set[str]:
    result = subprocess.run(
        [_UV, "run", "ruff", "check", "--output-format", "concise", str(target)],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return set(_CODE_RE.findall(result.stdout + result.stderr))


class TestEnablementPins:
    def test_c901_not_ignored_globally(self, ruff_lint: dict) -> None:
        assert "C901" not in _ignored_codes(ruff_lint)

    def test_mccabe_complexity_pinned(self, ruff_lint: dict) -> None:
        assert ruff_lint["mccabe"]["max-complexity"] == 10

    def test_fix_and_era_not_ignored_globally(self, ruff_lint: dict) -> None:
        assert not ({"FIX", "FIX002", "ERA", "ERA001"} & _ignored_codes(ruff_lint))

    def test_select_is_all(self, ruff_lint: dict) -> None:
        assert ruff_lint["select"] == ["ALL"]


@pytest.mark.integration
class TestCapsBite:
    def test_c901_flags_too_complex_function(self, tmp_path: Path) -> None:
        body = "\n".join(f"    if a == {i}:\n        return {i}" for i in range(15))
        probe = _REPO_ROOT / "src" / "teatree" / "_c901_probe.py"
        probe.write_text(f"def f(a: int) -> int:\n{body}\n    return -1\n", encoding="utf-8")
        try:
            assert "C901" in _ruff_codes(probe)
        finally:
            probe.unlink()

    def test_fix_flags_todo_comment(self, tmp_path: Path) -> None:
        probe = _REPO_ROOT / "src" / "teatree" / "_fix_probe.py"
        probe.write_text("x = 1  # TODO: wire this up\n", encoding="utf-8")
        try:
            assert "FIX002" in _ruff_codes(probe)
        finally:
            probe.unlink()

    def test_era_flags_commented_out_code(self, tmp_path: Path) -> None:
        probe = _REPO_ROOT / "src" / "teatree" / "_era_probe.py"
        probe.write_text("x = 1\n# y = compute(x, 2)\nprint(x)\n", encoding="utf-8")
        try:
            assert "ERA001" in _ruff_codes(probe)
        finally:
            probe.unlink()
