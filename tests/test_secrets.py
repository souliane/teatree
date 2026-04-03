"""Tests for ``teatree.utils.secrets.read_pass``."""

import subprocess
from unittest.mock import patch

from teatree.utils.secrets import read_pass


class TestReadPass:
    """Behaviour of the ``read_pass`` helper."""

    def test_returns_first_line_of_pass_output(self) -> None:
        """Successful invocation returns the first line, stripped."""
        completed = subprocess.CompletedProcess(
            args=["pass", "show", "my/secret"],
            returncode=0,
            stdout="s3cret-value\nmetadata line\n",
        )
        with patch("teatree.utils.secrets.subprocess.run", return_value=completed):
            assert read_pass("my/secret") == "s3cret-value"

    def test_returns_empty_string_on_called_process_error(self) -> None:
        """When ``pass`` exits non-zero, return empty string."""
        with patch(
            "teatree.utils.secrets.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "pass"),
        ):
            assert read_pass("missing/key") == ""

    def test_returns_empty_string_when_pass_not_installed(self) -> None:
        """When the ``pass`` binary is absent, return empty string."""
        with patch(
            "teatree.utils.secrets.subprocess.run",
            side_effect=FileNotFoundError("pass"),
        ):
            assert read_pass("any/key") == ""

    def test_returns_empty_string_on_empty_output(self) -> None:
        """When ``pass`` returns blank output, return empty string."""
        completed = subprocess.CompletedProcess(
            args=["pass", "show", "empty/key"],
            returncode=0,
            stdout="   \n",
        )
        with patch("teatree.utils.secrets.subprocess.run", return_value=completed):
            assert read_pass("empty/key") == ""
