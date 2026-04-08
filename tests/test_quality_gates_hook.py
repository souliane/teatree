"""Tests for the quality gate relaxation detection hook."""

import pytest

from scripts.hooks.check_quality_gates import _added_lines

# Unified diff format examples
_PYPROJECT_DIFF = """\
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -100,0 +101,2 @@
+  "S999",    # new ignore
+  "S603",
"""

_NOQA_DIFF = """\
diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -10,0 +11 @@
+    result = something()  # noqa: E501
"""

_PRAGMA_DIFF = """\
diff --git a/src/bar.py b/src/bar.py
--- a/src/bar.py
+++ b/src/bar.py
@@ -5,0 +6 @@
+    return None  # pragma: no cover
"""


class TestAddedLines:
    def test_extracts_added_lines_from_pyproject(self) -> None:
        results = _added_lines(_PYPROJECT_DIFF)
        assert len(results) == 2
        assert results[0] == ("pyproject.toml", 101, '  "S999",    # new ignore')
        assert results[1] == ("pyproject.toml", 102, '  "S603",')

    def test_extracts_noqa_from_code(self) -> None:
        results = _added_lines(_NOQA_DIFF)
        assert len(results) == 1
        assert results[0][0] == "src/foo.py"
        assert "# noqa" in results[0][2]

    def test_extracts_pragma_from_code(self) -> None:
        results = _added_lines(_PRAGMA_DIFF)
        assert len(results) == 1
        assert "pragma: no cover" in results[0][2]

    def test_empty_diff_returns_empty(self) -> None:
        assert _added_lines("") == []

    def test_ignores_removed_lines(self) -> None:
        diff = """\
diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -100,2 +100,0 @@
-  "S999",
-  "S603",
"""
        assert _added_lines(diff) == []

    def test_handles_single_line_hunk_header(self) -> None:
        diff = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1 @@
+new_line = True
"""
        results = _added_lines(diff)
        assert len(results) == 1
        assert results[0][1] == 1


class TestMainDetection:
    def test_returns_zero_when_no_relaxations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_quality_gates as mod  # noqa: PLC0415

        monkeypatch.setattr(mod, "_staged_diff", lambda path_filter="": "")
        assert mod.main() == 0

    def test_detects_pyproject_relaxation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_quality_gates as mod  # noqa: PLC0415

        def fake_diff(path_filter: str = "") -> str:
            if path_filter == "pyproject.toml":
                return _PYPROJECT_DIFF
            return ""

        monkeypatch.setattr(mod, "_staged_diff", fake_diff)
        assert mod.main() == 1

    def test_detects_noqa_in_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import scripts.hooks.check_quality_gates as mod  # noqa: PLC0415

        def fake_diff(path_filter: str = "") -> str:
            if path_filter == "pyproject.toml":
                return ""
            return _NOQA_DIFF

        monkeypatch.setattr(mod, "_staged_diff", fake_diff)
        assert mod.main() == 1
