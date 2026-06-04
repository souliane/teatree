"""Tests for the no-silent-skip pre-commit hook (ban unconditionally-disabled tests)."""

from pathlib import Path

import pytest

import scripts.hooks.check_no_silent_skip as mod


def _run_on(monkeypatch: pytest.MonkeyPatch, source: str, *, path: str = "tests/test_x.py") -> int:
    monkeypatch.setattr(mod, "_staged_test_files", lambda: [path])
    monkeypatch.setattr(mod, "_staged_source", lambda _f: source)
    return mod.main()


class TestBlocksSilentSkips:
    def test_unconditional_pytest_skip_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert True\n"
        assert _run_on(monkeypatch, source) == 1

    def test_pytest_skip_with_reason_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = 'import pytest\n@pytest.mark.skip(reason="flaky")\ndef test_a():\n    assert True\n'
        assert _run_on(monkeypatch, source) == 1

    def test_skipif_true_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = 'import pytest\n@pytest.mark.skipif(True, reason="off")\ndef test_a():\n    assert True\n'
        assert _run_on(monkeypatch, source) == 1

    def test_skipif_literal_one_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = "import pytest\n@pytest.mark.skipif(1)\ndef test_a():\n    assert True\n"
        assert _run_on(monkeypatch, source) == 1

    def test_skipif_not_false_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = "import pytest\n@pytest.mark.skipif(not False)\ndef test_a():\n    assert True\n"
        assert _run_on(monkeypatch, source) == 1

    def test_unittest_skip_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = 'import unittest\n@unittest.skip("disabled")\nclass T:\n    pass\n'
        assert _run_on(monkeypatch, source) == 1

    def test_disabled_class_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = "import pytest\n@pytest.mark.skip\nclass TestSuite:\n    def test_a(self):\n        assert True\n"
        assert _run_on(monkeypatch, source) == 1


class TestAllowsConditionalSkips:
    def test_skipif_runtime_condition_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = (
            "import shutil\nimport pytest\n"
            '@pytest.mark.skipif(shutil.which("git") is None, reason="git missing")\n'
            "def test_a():\n    assert True\n"
        )
        assert _run_on(monkeypatch, source) == 0

    def test_skipif_negated_name_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = (
            "import pytest\nMARKITDOWN_INSTALLED = False\n"
            "@pytest.mark.skipif(not MARKITDOWN_INSTALLED, reason='extra missing')\n"
            "def test_a():\n    assert True\n"
        )
        assert _run_on(monkeypatch, source) == 0

    def test_xfail_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = "import pytest\n@pytest.mark.xfail\ndef test_a():\n    assert False\n"
        assert _run_on(monkeypatch, source) == 0

    def test_plain_test_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = "def test_a():\n    assert True\n"
        assert _run_on(monkeypatch, source) == 0

    def test_non_test_path_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "_staged_test_files", list)
        assert mod.main() == 0


class TestTreeIsClean:
    def test_no_unconditional_skips_on_tree(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        offenders: list[str] = []
        for test_file in (repo_root / "tests").rglob("*.py"):
            rel = test_file.relative_to(repo_root).as_posix()
            offenders.extend(mod._violations_in_file(rel, test_file.read_text(encoding="utf-8")))
        assert not offenders, "\n".join(offenders)
