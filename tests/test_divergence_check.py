"""Tests for divergence_check.py."""

import json
from unittest.mock import patch

import pytest
from divergence_check import _analyze, main


class TestAnalyze:
    def _mock_git(self, mapping: dict[str, str]):  # noqa: ANN202
        def fake_run(*, repo: str = ".", args: list[str]) -> str:  # noqa: ARG001
            joined = " ".join(args)
            for key, val in mapping.items():
                if key in joined:
                    return val
            return ""

        return patch("divergence_check.git_run", side_effect=fake_run)

    def test_within_limits(self) -> None:
        fork_log = "\n".join(f"h{i} msg" for i in range(5))
        upstream_log = "\n".join(f"h{i} msg" for i in range(3))
        mapping = {
            "--abbrev-ref": "main",
            "show": "  HEAD branch: main\n",
            "merge-base": "abc123def456",
            "--format=%ci": "2026-01-01",
            "upstream/main..origin/main": fork_log,
            "origin/main..upstream/main": upstream_log,
        }
        with self._mock_git(mapping), patch("divergence_check.git_check", return_value=True):
            result = _analyze("/tmp", "owner/repo", "", max_fork_only=50, max_upstream_only=20)
        assert result["blocked"] is False
        assert result["fork_only"] == 5

    def test_blocked_when_too_diverged(self) -> None:
        fork_log = "\n".join(f"h{i} msg" for i in range(60))
        mapping = {
            "--abbrev-ref": "main",
            "show": "  HEAD branch: main\n",
            "merge-base": "abc123",
            "--format=%ci": "2026-01-01",
            "upstream/main..origin/main": fork_log,
        }
        with self._mock_git(mapping), patch("divergence_check.git_check", return_value=True):
            result = _analyze("/tmp", "owner/repo", "", max_fork_only=50, max_upstream_only=20)
        assert result["blocked"] is True

    def test_defaults_to_main_when_head_branch_missing(self) -> None:
        mapping = {
            "--abbrev-ref": "feat",
            "show": "  Fetch URL: https://github.com/o/r\n  Push URL: ...\n",
            "merge-base": "abc",
            "--format=%ci": "",
        }
        with self._mock_git(mapping), patch("divergence_check.git_check", return_value=True):
            result = _analyze("/tmp", "o/r", "feat", max_fork_only=50, max_upstream_only=20)
        assert "main" in str(result["upstream"])

    def test_skips_non_matching_lines(self) -> None:
        mapping = {
            "--abbrev-ref": "feat",
            "show": "  Fetch URL: x\n  HEAD branch: dev\n",
            "merge-base": "abc",
            "--format=%ci": "",
        }
        with self._mock_git(mapping), patch("divergence_check.git_check", return_value=True):
            result = _analyze("/tmp", "o/r", "feat", max_fork_only=50, max_upstream_only=20)
        assert "dev" in str(result["upstream"])

    def test_adds_upstream_remote_when_missing(self) -> None:
        mapping = {"--abbrev-ref": "main", "show": "  HEAD branch: main\n", "merge-base": "abc", "--format=%ci": ""}
        calls: list[str] = []

        def fake_run(*, repo: str = ".", args: list[str]) -> str:  # noqa: ARG001
            calls.append(" ".join(args))
            joined = " ".join(args)
            for key, val in mapping.items():
                if key in joined:
                    return val
            return ""

        with (
            patch("divergence_check.git_run", side_effect=fake_run),
            patch("divergence_check.git_check", return_value=False),
        ):
            _analyze("/tmp", "owner/repo", "main", max_fork_only=50, max_upstream_only=20)
        assert any("remote add upstream" in c for c in calls)


class TestMain:
    def _data(self, *, blocked: bool = False, fork_only: int = 0) -> dict:
        return {
            "blocked": blocked,
            "branch": "main",
            "upstream": "x/main",
            "merge_base": "abc",
            "merge_base_date": "",
            "fork_only": fork_only,
            "upstream_only": 0,
        }

    def test_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("divergence_check._analyze", return_value=self._data()):
            main("/tmp", upstream="o/r", branch="main", max_fork_only=50, max_upstream_only=20, json_output=True)
        assert json.loads(capsys.readouterr().out)["blocked"] is False

    def test_blocked_exits_1(self) -> None:
        with (
            patch("divergence_check._analyze", return_value=self._data(blocked=True, fork_only=60)),
            pytest.raises(SystemExit, match="1"),
        ):
            main("/tmp", upstream="o/r", branch="main", max_fork_only=50, max_upstream_only=20, json_output=False)

    def test_ok_prints_summary(self) -> None:
        with patch("divergence_check._analyze", return_value=self._data()):
            main("/tmp", upstream="o/r", branch="main", max_fork_only=50, max_upstream_only=20, json_output=False)
