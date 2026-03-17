"""Tests for lib.git — git helpers."""

from subprocess import CalledProcessError
from unittest.mock import MagicMock, patch

import pytest
from lib.git import check, default_branch, run


class TestDefaultBranch:
    def test_reads_symbolic_ref(self) -> None:
        with patch("lib.git.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="refs/remotes/origin/main\n",
            )
            assert default_branch("/repo") == "main"

    def test_falls_back_when_symbolic_ref_empty(self) -> None:
        """Branch 21->26: symbolic-ref returns empty string after stripping."""
        with patch("lib.git.subprocess.run") as mock_run:

            def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
                if "symbolic-ref" in args:
                    return MagicMock(
                        returncode=0,
                        stdout="refs/remotes/origin/\n",
                    )
                if "refs/remotes/origin/master" in args:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)

            mock_run.side_effect = side_effect
            assert default_branch("/repo") == "master"

    def test_falls_back_to_master(self) -> None:
        with patch("lib.git.subprocess.run") as mock_run:

            def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
                if "symbolic-ref" in args:
                    raise CalledProcessError(1, "git")
                if "refs/remotes/origin/master" in args:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)

            mock_run.side_effect = side_effect
            assert default_branch("/repo") == "master"

    def test_falls_back_to_development(self) -> None:
        with patch("lib.git.subprocess.run") as mock_run:

            def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
                if "symbolic-ref" in args:
                    raise CalledProcessError(1, "git")
                if "refs/remotes/origin/development" in args:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)

            mock_run.side_effect = side_effect
            assert default_branch("/repo") == "development"

    def test_raises_when_no_branch_found(self) -> None:
        with patch("lib.git.subprocess.run") as mock_run:

            def side_effect(args: list[str], **_kwargs: object) -> MagicMock:
                if "symbolic-ref" in args:
                    raise CalledProcessError(1, "git")
                return MagicMock(returncode=1)

            mock_run.side_effect = side_effect
            with pytest.raises(RuntimeError, match="Could not detect"):
                default_branch("/repo")


class TestRun:
    def test_returns_stripped_stdout(self) -> None:
        with patch("lib.git.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="  main  \n")
            assert run(repo="/tmp", args=["branch"]) == "main"

    def test_returns_empty_on_failure(self) -> None:
        with patch("lib.git.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            assert run(repo="/tmp", args=["bad-cmd"]) == ""


class TestCheck:
    def test_returns_true_on_success(self) -> None:
        with patch("lib.git.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert check(repo="/tmp", args=["status"]) is True

    def test_returns_false_on_failure(self) -> None:
        with patch("lib.git.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert check(repo="/tmp", args=["status"]) is False
