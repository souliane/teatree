"""Tests for _ports.py — port management helpers."""

import socket
from unittest.mock import MagicMock, patch

from lib.ports import free_port, port_in_use


class TestPortInUse:
    def test_returns_true_when_bound(self) -> None:
        """Bind a port then verify port_in_use detects it."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
        try:
            assert port_in_use(port) is True
        finally:
            sock.close()

    def test_returns_false_when_free(self) -> None:
        """Bind and release a port, then verify it's detected as free."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
        sock.close()
        assert port_in_use(port) is False


class TestFreePort:
    def test_returns_true_when_already_free(self) -> None:
        with patch("lib.ports.port_in_use", return_value=False):
            assert free_port(8000) is True

    def test_kills_process_and_returns_true(self) -> None:
        with (
            patch("lib.ports.subprocess.run") as mock_run,
            patch("lib.ports.port_in_use") as mock_in_use,
            patch("lib.ports.time.sleep"),
        ):
            # First call: port in use, second call (after kill): free
            mock_in_use.side_effect = [True, False]
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            assert free_port(8000) is True

    def test_kills_with_empty_stdout(self) -> None:
        """Branch 31->36: lsof returns no PIDs (empty stdout)."""
        with (
            patch("lib.ports.subprocess.run") as mock_run,
            patch("lib.ports.port_in_use") as mock_in_use,
            patch("lib.ports.time.sleep"),
        ):
            mock_in_use.side_effect = [True, False]
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            assert free_port(8000) is True

    def test_returns_false_when_cannot_free(self) -> None:
        with (
            patch("lib.ports.subprocess.run") as mock_run,
            patch("lib.ports.port_in_use", return_value=True),
            patch("lib.ports.time.sleep"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n")
            assert free_port(8000) is False
