"""Tests for verify_services.py."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from verify_services import (
    _check_endpoint,
    _load_custom_endpoints,
    verify,
)


class TestCheckEndpoint:
    def test_successful_check(self) -> None:
        with patch("verify_services.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="200", stderr="")
            result = _check_endpoint("localhost", 8000, "/admin/")
        assert result["ok"]
        assert result["status_code"] == 200
        assert result["url"] == "http://localhost:8000/admin/"
        assert result["error"] is None

    def test_failed_check(self) -> None:
        with patch("verify_services.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="500", stderr="")
            result = _check_endpoint("localhost", 8000, "/")
        assert not result["ok"]
        assert result["status_code"] == 500

    def test_connection_error(self) -> None:
        with patch("verify_services.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="Connection refused")
            result = _check_endpoint("localhost", 8000, "/")
        assert not result["ok"]
        assert result["status_code"] == 0
        assert result["error"] == "Connection refused"

    def test_non_digit_stdout(self) -> None:
        with patch("verify_services.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="curl: error", stderr="err")
            result = _check_endpoint("localhost", 8000, "/")
        assert result["status_code"] == 0

    def test_redirect_status(self) -> None:
        with patch("verify_services.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="302", stderr="")
            result = _check_endpoint("localhost", 8000, "/")
        assert result["ok"]

    def test_400_not_ok(self) -> None:
        with patch("verify_services.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="400", stderr="")
            result = _check_endpoint("localhost", 8000, "/")
        assert not result["ok"]


class TestLoadCustomEndpoints:
    def test_no_env(self) -> None:
        os.environ.pop("T3_HEALTH_ENDPOINTS", None)
        assert _load_custom_endpoints() is None

    def test_with_env(self) -> None:
        endpoints = {"api": {"port_env": "API_PORT", "path": "/health", "default_port": 3000}}
        os.environ["T3_HEALTH_ENDPOINTS"] = json.dumps(endpoints)
        try:
            result = _load_custom_endpoints()
            assert result == endpoints
        finally:
            del os.environ["T3_HEALTH_ENDPOINTS"]


class TestVerify:
    def test_default_endpoints(self) -> None:
        with (
            patch("verify_services.detect_ticket_dir", return_value=""),
            patch("verify_services._check_endpoint") as mock_check,
        ):
            mock_check.return_value = {
                "url": "http://localhost:8000/admin/login/",
                "status_code": 200,
                "ok": True,
                "error": None,
            }
            result = verify()
        assert "backend" in result
        assert "frontend" in result

    def test_explicit_backend_port(self) -> None:
        with (
            patch("verify_services.detect_ticket_dir", return_value=""),
            patch("verify_services._check_endpoint") as mock_check,
        ):
            mock_check.return_value = {"url": "x", "status_code": 200, "ok": True, "error": None}
            verify(backend_port=9000)
        # Check that backend was called with port 9000
        calls = mock_check.call_args_list
        backend_call = [c for c in calls if c[0][1] == 9000]
        assert len(backend_call) == 1

    def test_explicit_frontend_port(self) -> None:
        with (
            patch("verify_services.detect_ticket_dir", return_value=""),
            patch("verify_services._check_endpoint") as mock_check,
        ):
            mock_check.return_value = {"url": "x", "status_code": 200, "ok": True, "error": None}
            verify(frontend_port=5000)
        calls = mock_check.call_args_list
        frontend_call = [c for c in calls if c[0][1] == 5000]
        assert len(frontend_call) == 1

    def test_port_from_env_var(self) -> None:
        os.environ["BACKEND_PORT"] = "8888"
        try:
            with (
                patch("verify_services.detect_ticket_dir", return_value=""),
                patch("verify_services._check_endpoint") as mock_check,
            ):
                mock_check.return_value = {"url": "x", "status_code": 200, "ok": True, "error": None}
                verify()
            calls = mock_check.call_args_list
            backend_call = [c for c in calls if c[0][1] == 8888]
            assert len(backend_call) == 1
        finally:
            del os.environ["BACKEND_PORT"]

    def test_port_from_env_worktree(self, tmp_path: Path) -> None:
        td = tmp_path / "ticket"
        td.mkdir()
        env_file = td / ".env.worktree"
        env_file.write_text("BACKEND_PORT=7777\nFRONTEND_PORT=3333\n")
        with (
            patch("verify_services.detect_ticket_dir", return_value=str(td)),
            patch("verify_services._check_endpoint") as mock_check,
            patch("verify_services.read_env_key") as mock_read_env,
        ):
            port_map = {"BACKEND_PORT": "7777", "FRONTEND_PORT": "3333"}
            mock_read_env.side_effect = lambda _path, key: port_map.get(key, "")
            mock_check.return_value = {"url": "x", "status_code": 200, "ok": True, "error": None}
            verify()
        calls = mock_check.call_args_list
        backend_call = [c for c in calls if c[0][1] == 7777]
        assert len(backend_call) == 1

    def test_custom_endpoints(self) -> None:
        endpoints = {"api": {"port_env": "", "path": "/health", "default_port": 3000}}
        os.environ["T3_HEALTH_ENDPOINTS"] = json.dumps(endpoints)
        try:
            with (
                patch("verify_services.detect_ticket_dir", return_value=""),
                patch("verify_services._check_endpoint") as mock_check,
            ):
                mock_check.return_value = {"url": "x", "status_code": 200, "ok": True, "error": None}
                result = verify()
            assert "api" in result
            assert "backend" not in result
        finally:
            del os.environ["T3_HEALTH_ENDPOINTS"]

    def test_no_ticket_dir(self) -> None:
        with (
            patch("verify_services.detect_ticket_dir", return_value=""),
            patch("verify_services._check_endpoint") as mock_check,
        ):
            mock_check.return_value = {"url": "x", "status_code": 200, "ok": True, "error": None}
            result = verify()
        assert len(result) == 2
