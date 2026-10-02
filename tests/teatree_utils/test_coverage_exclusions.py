"""Tests for vendored-path coverage exclusions (issue #1873)."""

import pytest

from teatree.utils.coverage_exclusions import VENDORED_PATTERNS, is_vendored_path, recompute_percent


class TestIsVendoredPath:
    def test_venv_excluded(self):
        assert is_vendored_path(".venv/lib/python3.12/site-packages/foo.py")

    def test_nested_venv_excluded(self):
        assert is_vendored_path("sub/.venv/bin/x.py")

    def test_site_packages_excluded(self):
        assert is_vendored_path("/usr/lib/python3.12/site-packages/numpy/core.py")

    def test_node_modules_excluded(self):
        assert is_vendored_path("frontend/node_modules/react/index.js")

    def test_repo_source_not_excluded(self):
        assert not is_vendored_path("src/teatree/cli/assess.py")

    def test_substring_not_a_segment_not_excluded(self):
        # "venv" appearing inside a real source filename must NOT match;
        # only the directory segments .venv/site-packages/node_modules count.
        assert not is_vendored_path("src/myvenv_helper.py")
        assert not is_vendored_path("src/node_modules_loader.py")

    def test_all_documented_patterns_present(self):
        assert ".venv" in VENDORED_PATTERNS
        assert "site-packages" in VENDORED_PATTERNS
        assert "node_modules" in VENDORED_PATTERNS


class TestRecomputePercent:
    def _files(self) -> dict:
        # 100% covered repo file, 0% covered vendored file. The unfiltered
        # total would be 50%; excluding the vendored file must give 100%.
        return {
            "src/teatree/a.py": {"summary": {"covered_lines": 10, "num_statements": 10}},
            ".venv/lib/site-packages/dep.py": {"summary": {"covered_lines": 0, "num_statements": 10}},
        }

    def test_excludes_vendored_from_total(self):
        result = recompute_percent(self._files())
        assert result["available"] is True
        assert result["percent"] == pytest.approx(100.0)

    def test_no_vendored_files_unchanged(self):
        files = {
            "src/teatree/a.py": {"summary": {"covered_lines": 8, "num_statements": 10}},
            "src/teatree/b.py": {"summary": {"covered_lines": 2, "num_statements": 10}},
        }
        result = recompute_percent(files)
        assert result["available"] is True
        assert result["percent"] == pytest.approx(50.0)

    def test_all_vendored_means_unavailable(self):
        files = {
            ".venv/x.py": {"summary": {"covered_lines": 0, "num_statements": 5}},
            "node_modules/y.js": {"summary": {"covered_lines": 0, "num_statements": 5}},
        }
        result = recompute_percent(files)
        assert result["available"] is False

    def test_empty_files_unavailable(self):
        result = recompute_percent({})
        assert result["available"] is False

    def test_zero_statements_after_filter_unavailable(self):
        files = {"src/teatree/empty.py": {"summary": {"covered_lines": 0, "num_statements": 0}}}
        result = recompute_percent(files)
        assert result["available"] is False
