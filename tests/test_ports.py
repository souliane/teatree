"""Tests for _ports.py — port management helpers."""

from unittest.mock import MagicMock, patch

from lib.ports import free_port, port_in_use


class TestPortInUse:
    def test_returns_true_when_listening(self) -> None:
        with patch("lib.ports.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert port_in_use(8000) is True

    def test_returns_false_when_not_listening(self) -> None:
        with patch("lib.ports.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert port_in_use(8000) is False

    def test_checks_correct_port(self) -> None:
        with patch("lib.ports.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            port_in_use(9999)
            args = mock_run.call_args.args[0]
            assert ":9999" in args


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
