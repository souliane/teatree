"""Tests for run_e2e.py — E2E test runner with automatic environment setup."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from run_e2e import (
    _abort_missing,
    _ensure_ready,
    _ensure_services_or_fail,
    _find_test_dir,
    _report_artifacts,
    _run_playwright,
    _verify_services,
    main,
)


class TestAbortMissing:
    def test_exits_with_message(self) -> None:
        with pytest.raises(SystemExit, match="1"):
            _abort_missing("variant (customer name)")


class TestEnsureReady:
    def test_advances_from_created(self) -> None:
        lc = MagicMock(state="created")

        def set_provisioned(*_a: object, **_kw: object) -> None:
            lc.state = "provisioned"

        def set_services_up() -> None:
            lc.state = "services_up"

        def set_ready() -> None:
            lc.state = "ready"

        lc.provision.side_effect = set_provisioned
        lc.start_services.side_effect = set_services_up
        lc.verify.side_effect = set_ready

        with patch("run_e2e.resolve_context") as mock_ctx:
            mock_ctx.return_value = MagicMock(wt_dir="/tmp/wt", main_repo="/tmp/repo")
            _ensure_ready(lc, "customer")

        lc.provision.assert_called_once()
        lc.start_services.assert_called_once()
        lc.verify.assert_called_once()

    def test_advances_from_provisioned(self) -> None:
        lc = MagicMock(state="provisioned")

        def set_services_up() -> None:
            lc.state = "services_up"

        def set_ready() -> None:
            lc.state = "ready"

        lc.start_services.side_effect = set_services_up
        lc.verify.side_effect = set_ready

        _ensure_ready(lc, "customer")
        lc.provision.assert_not_called()
        lc.start_services.assert_called_once()

    def test_noop_when_already_ready(self) -> None:
        lc = MagicMock(state="ready")
        _ensure_ready(lc, "customer")
        lc.provision.assert_not_called()
        lc.start_services.assert_not_called()
        lc.verify.assert_not_called()

    def test_exits_on_unexpected_state(self) -> None:
        lc = MagicMock(state="broken")
        with pytest.raises(SystemExit, match="1"):
            _ensure_ready(lc, "customer")


class TestVerifyServices:
    def test_passes_when_services_respond(self) -> None:
        with patch("run_e2e.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="200")
            assert _verify_services({"backend": 8001, "frontend": 4201}) is True

    def test_fails_when_service_unreachable(self) -> None:
        with patch("run_e2e.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="000")
            assert _verify_services({"backend": 8001, "frontend": 4201}) is False

    def test_handles_empty_response(self) -> None:
        with patch("run_e2e.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            assert _verify_services({"backend": 8001}) is False

    def test_skips_missing_ports(self) -> None:
        with patch("run_e2e.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="200")
            assert _verify_services({}) is True
            mock_run.assert_not_called()


class TestFindTestDir:
    def test_returns_private_tests_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        test_dir = tmp_path / "e2e"
        test_dir.mkdir()
        monkeypatch.setenv("T3_PRIVATE_TESTS", str(test_dir))
        assert _find_test_dir() == test_dir

    def test_returns_none_when_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_PRIVATE_TESTS", raising=False)
        assert _find_test_dir() is None

    def test_returns_none_when_dir_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_PRIVATE_TESTS", "/nonexistent/path")
        assert _find_test_dir() is None


class TestRunPlaywright:
    def test_runs_headless_by_default(self, tmp_path: Path) -> None:
        with patch("run_e2e.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            result = _run_playwright(
                tmp_path,
                {"frontend": 4201},
                spec="",
                variant="customer",
                app_name="brokerage",
                headed=False,
            )
        assert result.returncode == 0
        call_kwargs = mock_run.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env", {})
        assert env["CI"] == "1"
        assert env["BASE_URL"] == "http://localhost:4201"

    def test_runs_headed_when_requested(self, tmp_path: Path) -> None:
        with patch("run_e2e.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
            _run_playwright(
                tmp_path,
                {"frontend": 4201},
                spec="tests/login.spec.ts",
                variant="customer",
                app_name="self-service",
                headed=True,
            )
        cmd = mock_run.call_args[0][0]
        assert "--headed" in cmd
        assert "tests/login.spec.ts" in cmd
        env = mock_run.call_args.kwargs.get("env") or mock_run.call_args[1].get("env", {})
        assert env["CI"] == "0"
        assert env["APP"] == "self-service"


class TestEnsureServicesOrFail:
    def test_passes_when_services_respond(self) -> None:
        lc = MagicMock()
        with patch("run_e2e._verify_services", return_value=True):
            _ensure_services_or_fail(lc, {"backend": 8001}, skip_setup=False)

    def test_restarts_when_not_responding(self) -> None:
        call_count = 0

        def verify(ports: dict) -> bool:  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            return call_count > 1

        lc = MagicMock(state="ready")
        with patch("run_e2e._verify_services", side_effect=verify):
            _ensure_services_or_fail(lc, {"backend": 8001}, skip_setup=False)
        lc.start_services.assert_called_once()

    def test_exits_when_skip_setup_and_down(self) -> None:
        lc = MagicMock()
        with patch("run_e2e._verify_services", return_value=False), pytest.raises(SystemExit, match="1"):
            _ensure_services_or_fail(lc, {"backend": 8001}, skip_setup=True)

    def test_exits_when_restart_fails(self) -> None:
        lc = MagicMock(state="ready")
        with patch("run_e2e._verify_services", return_value=False), pytest.raises(SystemExit, match="1"):
            _ensure_services_or_fail(lc, {"backend": 8001}, skip_setup=False)


class TestReportArtifacts:
    def test_reports_when_artifacts_exist(self, tmp_path: Path) -> None:
        results = tmp_path / "test-results"
        results.mkdir()
        (results / "video.webm").touch()
        (results / "screenshot.png").touch()
        _report_artifacts(tmp_path)

    def test_silent_when_no_artifacts(self, tmp_path: Path) -> None:
        _report_artifacts(tmp_path)

    def test_silent_when_dir_empty(self, tmp_path: Path) -> None:
        (tmp_path / "test-results").mkdir()
        _report_artifacts(tmp_path)


class TestMain:
    def test_exits_when_not_in_ticket_dir(self) -> None:
        with patch("run_e2e.detect_ticket_dir", return_value=""), pytest.raises(SystemExit, match="1"):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=False)

    def test_exits_when_no_test_dir(self) -> None:
        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=None),
            pytest.raises(SystemExit, match="1"),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=False)

    def test_exits_when_no_ports(self) -> None:
        lc = MagicMock(state="ready", facts={"variant": "customer"})
        lc.facts = {"variant": "customer"}  # no ports
        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=Path("/tmp/e2e")),
            patch("run_e2e.WorktreeLifecycle", return_value=lc),
            pytest.raises(SystemExit, match="1"),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=True)

    def test_full_flow_passes(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "e2e"
        test_dir.mkdir()
        results_dir = test_dir / "test-results"
        results_dir.mkdir()
        (results_dir / "video.webm").touch()
        (results_dir / "screenshot.png").touch()

        lc = MagicMock(
            state="ready",
            facts={"variant": "customer", "ports": {"backend": 8001, "frontend": 4201}},
        )

        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=test_dir),
            patch("run_e2e.WorktreeLifecycle", return_value=lc),
            patch("run_e2e._verify_services", return_value=True),
            patch("run_e2e._run_playwright", return_value=MagicMock(returncode=0)),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=True)

    def test_full_flow_fails(self) -> None:
        lc = MagicMock(
            state="ready",
            facts={"variant": "customer", "ports": {"backend": 8001, "frontend": 4201}},
        )
        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=Path("/tmp/e2e")),
            patch("run_e2e.WorktreeLifecycle", return_value=lc),
            patch("run_e2e._verify_services", return_value=True),
            patch("run_e2e._run_playwright", return_value=MagicMock(returncode=1)),
            pytest.raises(SystemExit, match="1"),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=True)

    def test_restarts_services_when_not_responding(self) -> None:
        lc = MagicMock(
            state="ready",
            facts={"variant": "customer", "ports": {"backend": 8001, "frontend": 4201}},
        )

        call_count = 0

        def verify_side_effect(ports: dict) -> bool:  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            return call_count > 1  # fail first, pass second

        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=Path("/tmp/e2e")),
            patch("run_e2e.WorktreeLifecycle", return_value=lc),
            patch("run_e2e._verify_services", side_effect=verify_side_effect),
            patch("run_e2e._run_playwright", return_value=MagicMock(returncode=0)),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=False)

        lc.start_services.assert_called_once()

    def test_skip_setup_warns_on_wrong_state(self) -> None:
        lc = MagicMock(
            state="provisioned",
            facts={"variant": "customer", "ports": {"backend": 8001, "frontend": 4201}},
        )
        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=Path("/tmp/e2e")),
            patch("run_e2e.WorktreeLifecycle", return_value=lc),
            patch("run_e2e._verify_services", return_value=True),
            patch("run_e2e._run_playwright", return_value=MagicMock(returncode=0)),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=True)

    def test_exits_when_variant_missing(self) -> None:
        lc = MagicMock(
            state="ready",
            facts={"ports": {"backend": 8001, "frontend": 4201}},
        )
        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=Path("/tmp/e2e")),
            patch("run_e2e.WorktreeLifecycle", return_value=lc),
            patch("run_e2e._verify_services", return_value=True),
            pytest.raises(SystemExit, match="1"),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=True)

    def test_exits_when_services_fail_after_restart(self) -> None:
        lc = MagicMock(
            state="ready",
            facts={"variant": "customer", "ports": {"backend": 8001, "frontend": 4201}},
        )
        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=Path("/tmp/e2e")),
            patch("run_e2e.WorktreeLifecycle", return_value=lc),
            patch("run_e2e._verify_services", return_value=False),
            pytest.raises(SystemExit, match="1"),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=False)

    def test_exits_when_skip_setup_and_services_down(self) -> None:
        lc = MagicMock(
            state="ready",
            facts={"variant": "customer", "ports": {"backend": 8001, "frontend": 4201}},
        )
        with (
            patch("run_e2e.detect_ticket_dir", return_value="/tmp/ticket"),
            patch("run_e2e._find_test_dir", return_value=Path("/tmp/e2e")),
            patch("run_e2e.WorktreeLifecycle", return_value=lc),
            patch("run_e2e._verify_services", return_value=False),
            pytest.raises(SystemExit, match="1"),
        ):
            main("", variant="", app_name="brokerage", headed=False, skip_setup=True)
