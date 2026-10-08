"""The doctor's Codex line: login present or missing, its age, the last long hold, a route past the outage."""

import io
import os
import tempfile
from contextlib import AbstractContextManager, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_codex_login import check_codex_login
from teatree.core.models import AgentRouteAvailability, UsageWindowState
from tests.teatree_agents._route_fakes import route_config

_SENTINEL = "sentinel-do-not-print"
_ROUTE = route_config("code", "codex_app_server")


def _run() -> str:
    stream = io.StringIO()
    with redirect_stdout(stream):
        assert check_codex_login() is True, "the Codex line is advisory and never reddens the run"
    return stream.getvalue()


class TestCodexLoginDoctorLine(TestCase):
    def setUp(self) -> None:
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"T3_CODEX_HOME": str(self.home)}))

    @staticmethod
    def _route() -> AbstractContextManager[object]:
        return patch("teatree.config.agent_spawn.resolve_agent_config", return_value=_ROUTE)

    def _login(self) -> Path:
        path = self.home / "auth.json"
        path.write_text(f'{{"tokens": {{"access_token": "{_SENTINEL}"}}}}')
        return path

    def test_it_is_silent_when_codex_is_not_in_play(self) -> None:
        assert _run() == ""

    def test_a_present_login_reports_its_mtime_and_never_its_contents(self) -> None:
        path = self._login()

        output = _run()

        assert "Codex login present" in output
        assert datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d") in output
        assert _SENTINEL not in output

    def test_a_route_with_no_login_warns_and_names_the_import_command(self) -> None:
        with self._route():
            output = _run()

        assert "WARN" in output
        assert "Codex login missing" in output
        assert "t3 codex auth import" in output

    def test_the_last_quota_or_auth_hold_is_named(self) -> None:
        self._login()
        now = timezone.now()
        AgentRouteAvailability.objects.create(
            harness="codex_app_server",
            model="gpt-6-sol",
            unavailable_reason="Codex provider reported a retryable quota failure during turn/completed.",
            observed_at=now,
            retry_at=now + timedelta(hours=1),
        )

        output = _run()

        assert "last hold" in output
        assert "retryable quota failure" in output

    def test_a_managed_route_outliving_the_usage_window_warns_until_it_is_cleared(self) -> None:
        self._login()
        with self._route():
            assert "outlived the usage window" in _run()

            UsageWindowState.record_limit(
                lane="", cause="subscription_session", resets_at=timezone.now() + timedelta(hours=2)
            )

            assert "outlived the usage window" not in _run()
