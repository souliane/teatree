import logging
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.config.settings import UserSettings
from teatree.core.management.commands import _test_plan
from teatree.core.test_plan_blocked_gate import (
    BlockedTestPlanPostError,
    check_blocked_body,
    check_blocked_body_from_config,
)
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}

_FAKE_COLLEAGUE_URL = "https://gitlab.com/fake-corp/main-app/-/issues/123"
_FAKE_SOLO_URL = "https://gitlab.com/fake-owner/my-solo-tool/-/issues/99"
_IRRELEVANT_URL = "https://gitlab.com/some-org/some-repo/-/issues/7"

_FAKE_COLLEAGUE_RE = re.compile(r"https://gitlab\.com/fake-corp/(?:main-app|other-app)/")
_FAKE_SOLO_RE = re.compile(r"https://gitlab\.com/fake-owner/my-solo-tool(?:-e2e)?/")

_CLEAN_BODY = "## E2E Evidence\n\nAll workflows passed on dev and local.\n"
_BLOCKED_BODY = "## E2E Evidence\n\nUnable to test the login flow on DEV.\n"


class TestCheckBlockedBodyMustRefuse:
    def test_raises_for_colleague_url_with_blocked_phrase(self) -> None:
        with pytest.raises(BlockedTestPlanPostError, match="blocked phrase"):
            check_blocked_body(
                _BLOCKED_BODY, _FAKE_COLLEAGUE_URL, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE
            )

    def test_anti_vacuity_gate_removed_means_red(self) -> None:
        with pytest.raises(BlockedTestPlanPostError):
            check_blocked_body(
                "unable to test the flow", _FAKE_COLLEAGUE_URL, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE
            )

    def test_all_blocked_phrases_trigger_refusal(self) -> None:
        phrases = [
            "unable to test",
            "could not test",
            "couldn't test",
            "blocked",
            "DEV verification pending",
            "verification pending",
            "not verified",
            "pending cred",
            "not automatable",
            "was unable to",
        ]
        for phrase in phrases:
            with pytest.raises(BlockedTestPlanPostError, match="blocked phrase"):
                check_blocked_body(
                    f"Step result: {phrase}.",
                    _FAKE_COLLEAGUE_URL,
                    colleague_re=_FAKE_COLLEAGUE_RE,
                    solo_re=_FAKE_SOLO_RE,
                )

    def test_match_is_case_insensitive(self) -> None:
        with pytest.raises(BlockedTestPlanPostError):
            check_blocked_body(
                "UNABLE TO TEST the payment flow.",
                _FAKE_COLLEAGUE_URL,
                colleague_re=_FAKE_COLLEAGUE_RE,
                solo_re=_FAKE_SOLO_RE,
            )

    def test_raises_for_second_colleague_slug(self) -> None:
        url = "https://gitlab.com/fake-corp/other-app/-/issues/1"
        with pytest.raises(BlockedTestPlanPostError):
            check_blocked_body(_BLOCKED_BODY, url, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE)


class TestCheckBlockedBodyMustAllow:
    def test_clean_body_colleague_allows(self) -> None:
        check_blocked_body(_CLEAN_BODY, _FAKE_COLLEAGUE_URL, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE)

    def test_clean_body_solo_allows(self) -> None:
        check_blocked_body(_CLEAN_BODY, _FAKE_SOLO_URL, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE)

    def test_blocked_body_solo_warns_not_refuses(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="teatree.core.test_plan_blocked_gate"):
            check_blocked_body(_BLOCKED_BODY, _FAKE_SOLO_URL, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE)
        assert any("blocked phrase" in r.message for r in caplog.records)

    def test_blocked_body_solo_e2e_warns_not_refuses(self, caplog: pytest.LogCaptureFixture) -> None:
        url = "https://gitlab.com/fake-owner/my-solo-tool-e2e/-/issues/5"
        with caplog.at_level(logging.WARNING, logger="teatree.core.test_plan_blocked_gate"):
            check_blocked_body(_BLOCKED_BODY, url, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE)
        assert any("blocked phrase" in r.message for r in caplog.records)

    def test_blocked_body_irrelevant_org_allows(self) -> None:
        check_blocked_body(_BLOCKED_BODY, _IRRELEVANT_URL, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE)

    def test_no_patterns_configured_allows_any_body(self) -> None:
        check_blocked_body(_BLOCKED_BODY, _FAKE_COLLEAGUE_URL, colleague_re=None, solo_re=None)


def _fake_settings() -> UserSettings:
    settings = UserSettings()
    settings.colleague_repo_url_pattern = r"https://gitlab\.com/fake-corp/(?:main-app|other-app)/"
    settings.solo_repo_url_pattern = r"https://gitlab\.com/fake-owner/my-solo-tool(?:-e2e)?/"
    return settings


class TestCheckBlockedBodyFromConfig:
    def test_refuses_colleague_url_via_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import teatree.core.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)
        with pytest.raises(BlockedTestPlanPostError):
            check_blocked_body_from_config(_BLOCKED_BODY, _FAKE_COLLEAGUE_URL)

    def test_allows_clean_body_via_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import teatree.core.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)
        check_blocked_body_from_config(_CLEAN_BODY, _FAKE_COLLEAGUE_URL)

    def test_anti_vacuity_config_wrapper_goes_red_without_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import teatree.core.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)
        with pytest.raises(BlockedTestPlanPostError):
            check_blocked_body_from_config("unable to test the flow", _FAKE_COLLEAGUE_URL)


class TestBlockedGateAtBodyFilePath(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path
        disable_on_behalf_gate(tmp_path_factory, monkeypatch)
        import teatree.core.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)

    def _host(self) -> MagicMock:
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.post_issue_comment.return_value = {"id": 1, "web_url": "u"}
        return host

    def test_must_refuse_colleague_url_blocked_body(self) -> None:
        host = self._host()
        with pytest.raises(BlockedTestPlanPostError):
            _test_plan.post_body_file_comment(
                host,
                issue_url=_FAKE_COLLEAGUE_URL,
                ticket_id="123",
                body=_BLOCKED_BODY,
            )
        host.post_issue_comment.assert_not_called()

    def test_must_allow_colleague_url_clean_body(self) -> None:
        host = self._host()
        result = _test_plan.post_body_file_comment(
            host,
            issue_url=_FAKE_COLLEAGUE_URL,
            ticket_id="123",
            body=_CLEAN_BODY,
        )
        assert result["action"] == "created"
        host.post_issue_comment.assert_called_once()

    def test_must_allow_solo_url_blocked_body(self) -> None:
        host = self._host()
        result = _test_plan.post_body_file_comment(
            host,
            issue_url=_FAKE_SOLO_URL,
            ticket_id="99",
            body=_BLOCKED_BODY,
        )
        assert result["action"] == "created"
        host.post_issue_comment.assert_called_once()

    def test_run_post_test_plan_body_file_exits_nonzero_on_blocked_colleague(self) -> None:
        host = self._host()
        self._monkeypatch.setattr(_test_plan, "code_host_from_overlay", lambda: host)
        self._monkeypatch.setattr(_test_plan, "_resolve_worktree_or_none", lambda: None)
        ticket = MagicMock()
        ticket.issue_url = _FAKE_COLLEAGUE_URL
        ticket.ticket_number = "123"
        self._monkeypatch.setattr("teatree.core.models.Ticket.objects.resolve", lambda *a, **kw: ticket)

        from django.core.management import call_command  # noqa: PLC0415

        body_path = self._tmp / "blocked.md"
        body_path.write_text(_BLOCKED_BODY, encoding="utf-8")

        with (
            pytest.raises(SystemExit) as exc_info,
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
        ):
            call_command("e2e", "post-test-plan", ticket=_FAKE_COLLEAGUE_URL, body_file=str(body_path))
        assert exc_info.value.code != 0
        host.post_issue_comment.assert_not_called()
