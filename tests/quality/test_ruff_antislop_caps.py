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

FIX and TD are also the commit-time half of the deferred-marker ban on the
verification trees, whose whole statement is
``tests/quality/test_deferred_marker_ban.py`` and whose vocabulary is the
registry that gate reads (which pins the vocabulary parity from its side). Ruff
resolves an exemption by path, so those two rules are pinned out of
``per-file-ignores`` as well and probed at a ``tests/`` and an ``e2e/`` filename
rather than only under ``tmp_path``.
"""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from tests._color_env import no_color_env

_CODE_RE = re.compile(r"\b([A-Z]+\d+)\b")

#: The rule families that make a deferred marker in a comment a lint error.
_MARKER_RULES = frozenset({"FIX", "FIX001", "FIX002", "FIX003", "FIX004", "TD", "TD001", "TD002", "TD003", "TD004"})

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


def _per_file_ignored_codes(lint: dict) -> dict[str, set[str]]:
    return {glob: set(codes) for glob, codes in lint.get("per-file-ignores", {}).items()}


def _ruff_codes_for_filename(source: str, filename: str) -> set[str]:
    # Ruff resolves `per-file-ignores` against the DECLARED path, so a probe on
    # disk under tmp_path can never show whether a rule still bites at
    # `tests/**` or `e2e/**`. `--stdin-filename` asks that question without
    # writing into the shared tree.
    result = subprocess.run(
        [
            _UV,
            "run",
            "ruff",
            "check",
            "--config",
            str(_PYPROJECT),
            "--no-cache",
            "--no-fix",
            "--output-format",
            "concise",
            "--color=never",
            "--stdin-filename",
            filename,
            "-",
        ],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        input=source,
        env=no_color_env(),
    )
    return set(_CODE_RE.findall(result.stdout + result.stderr))


def _ruff_codes(target: Path) -> set[str]:
    # ``--config <repo pyproject>`` applies the real project ruff config to a
    # probe that lives OUTSIDE the tree (under tmp_path) — so the caps still bite
    # without a probe ever being written into the live ``src/teatree/`` tree
    # (the shared-tree-mutation flake the relocation removes). ``--no-cache`` so a
    # stale ruff cache keyed on a prior probe path never masks the result.
    # --color=never plus a color-forcing-stripped env: belt and suspenders
    # against an ambient FORCE_COLOR/CLICOLOR_FORCE ANSI-wrapping ruff's
    # output, which breaks \b-bounded code extraction (souliane/teatree#2359).
    result = subprocess.run(
        [
            _UV,
            "run",
            "ruff",
            "check",
            "--config",
            str(_PYPROJECT),
            "--no-cache",
            "--output-format",
            "concise",
            "--color=never",
            str(target),
        ],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=no_color_env(),
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

    def test_no_path_exempts_itself_from_the_marker_rules(self, ruff_lint: dict) -> None:
        # `lint.ignore` is not the only way to switch a rule off. A
        # `per-file-ignores` entry for `tests/**/*.py` would drop the fast
        # commit-time half of the marker ban and leave only the CI-lane gate,
        # silently.
        relaxed = {
            glob: codes & _MARKER_RULES
            for glob, codes in _per_file_ignored_codes(ruff_lint).items()
            if codes & _MARKER_RULES
        }
        assert not relaxed, f"a per-file-ignores entry disables the deferred-marker rules: {relaxed}"


@pytest.mark.integration
class TestCapsBiteInTheVerificationTrees:
    """The commit-time half of the deferred-marker ban, asked at the real paths."""

    @pytest.mark.parametrize("filename", ["tests/quality/_probe.py", "e2e/dash/_probe.py"])
    def test_a_marker_comment_is_still_flagged(self, filename: str) -> None:
        assert "FIX002" in _ruff_codes_for_filename("x = 1  # TODO: wire this up\n", filename)

    def test_a_string_literal_carrying_the_word_is_not(self) -> None:
        codes = _ruff_codes_for_filename('LABEL = "renders TODO list page"\n', "tests/quality/_probe.py")
        assert not ({"FIX001", "FIX002"} & codes)


@pytest.mark.integration
class TestCapsBite:
    @pytest.fixture(autouse=True)
    def _force_color(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Exercise the color-forced path on every run (not just a dev shell
        # that happens to set it) so the extractor's hermeticity is proven,
        # not merely assumed (souliane/teatree#2359).
        monkeypatch.setenv("FORCE_COLOR", "1")

    def test_c901_flags_too_complex_function(self, tmp_path: Path) -> None:
        body = "\n".join(f"    if a == {i}:\n        return {i}" for i in range(15))
        probe = tmp_path / "_c901_probe.py"
        probe.write_text(f"def f(a: int) -> int:\n{body}\n    return -1\n", encoding="utf-8")
        assert "C901" in _ruff_codes(probe)

    def test_fix_flags_todo_comment(self, tmp_path: Path) -> None:
        probe = tmp_path / "_fix_probe.py"
        probe.write_text("x = 1  # TODO: wire this up\n", encoding="utf-8")
        assert "FIX002" in _ruff_codes(probe)

    def test_era_flags_commented_out_code(self, tmp_path: Path) -> None:
        probe = tmp_path / "_era_probe.py"
        probe.write_text("x = 1\n# y = compute(x, 2)\nprint(x)\n", encoding="utf-8")
        assert "ERA001" in _ruff_codes(probe)
