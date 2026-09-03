import json
import logging
import re
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config.settings import UserSettings
from teatree.core.evidence.test_plan_blocked_gate import (
    BlockedTestPlanPostError,
    check_blocked_body,
    check_blocked_body_from_config,
)
from teatree.core.management.commands._test_plan import write as _write
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayMetadata
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate
from tests.teatree_core.conftest import CommandOverlay

_E2E_REPO = "client-workspace"


class _E2eRepoMetadata(OverlayMetadata):
    def get_e2e_config(self) -> dict[str, str]:
        return {"runner": "external", "project_path": f"org/{_E2E_REPO}", "e2e_dir": "e2e"}


class _E2eRepoOverlay(CommandOverlay):
    metadata = _E2eRepoMetadata()

    def get_repos(self) -> list[str]:
        return [_E2E_REPO]


_MOCK_OVERLAY = {"test": _E2eRepoOverlay()}


def _seed(issue_url: str, checkout: Path) -> Ticket:
    """A ticket whose e2e-repo worktree is a real directory, so the plan path resolves."""
    ticket = Ticket.objects.create(overlay="test", issue_url=issue_url)
    checkout.mkdir(parents=True, exist_ok=True)
    Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path=_E2E_REPO,
        branch="123-feat-thing",
        extra={"worktree_path": str(checkout)},
    )
    return ticket


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
            "waiting for",
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
        with caplog.at_level(logging.WARNING, logger="teatree.core.evidence.test_plan_blocked_gate"):
            check_blocked_body(_BLOCKED_BODY, _FAKE_SOLO_URL, colleague_re=_FAKE_COLLEAGUE_RE, solo_re=_FAKE_SOLO_RE)
        assert any("blocked phrase" in r.message for r in caplog.records)

    def test_blocked_body_solo_e2e_warns_not_refuses(self, caplog: pytest.LogCaptureFixture) -> None:
        url = "https://gitlab.com/fake-owner/my-solo-tool-e2e/-/issues/5"
        with caplog.at_level(logging.WARNING, logger="teatree.core.evidence.test_plan_blocked_gate"):
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
        import teatree.core.evidence.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)
        with pytest.raises(BlockedTestPlanPostError):
            check_blocked_body_from_config(_BLOCKED_BODY, _FAKE_COLLEAGUE_URL)

    def test_allows_clean_body_via_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import teatree.core.evidence.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)
        check_blocked_body_from_config(_CLEAN_BODY, _FAKE_COLLEAGUE_URL)

    def test_anti_vacuity_config_wrapper_goes_red_without_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import teatree.core.evidence.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)
        with pytest.raises(BlockedTestPlanPostError):
            check_blocked_body_from_config("unable to test the flow", _FAKE_COLLEAGUE_URL)


class TestBlockedGateAtBodyFilePath(TestCase):
    """The free-text ``--body-file`` plan is scanned before it lands in the e2e repo."""

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
        import teatree.core.evidence.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)
        monkeypatch.setattr(_write, "_resolve_worktree_or_none", lambda: None)
        self.enterContext(patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY))

    def _write_body_file(self, *, issue_url: str, body: str) -> str:
        _seed(issue_url, self._tmp / issue_url.rsplit("/", 1)[-1])
        path = self._tmp / "plan.md"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def _plan_path(self, issue_url: str) -> Path:
        number = issue_url.rsplit("/", 1)[-1]
        repo = issue_url.split("/-/", maxsplit=1)[0].rsplit("/", 1)[-1]
        return self._tmp / number / "test-plans" / f"{repo}-{number}.md"

    def _run(self, *, issue_url: str, body: str) -> None:
        body_file = self._write_body_file(issue_url=issue_url, body=body)
        _write.run_write_test_plan(
            _write.TestPlanFlags(ticket=issue_url, body_file=body_file),
            write_err=lambda _s: None,
        )

    def test_must_refuse_colleague_url_blocked_body(self) -> None:
        with pytest.raises(SystemExit):
            self._run(issue_url=_FAKE_COLLEAGUE_URL, body=_BLOCKED_BODY)
        assert not self._plan_path(_FAKE_COLLEAGUE_URL).exists()

    def test_must_allow_colleague_url_clean_body(self) -> None:
        self._run(issue_url=_FAKE_COLLEAGUE_URL, body=_CLEAN_BODY)
        assert self._plan_path(_FAKE_COLLEAGUE_URL).read_text(encoding="utf-8") == _CLEAN_BODY

    def test_must_allow_solo_url_blocked_body(self) -> None:
        self._run(issue_url=_FAKE_SOLO_URL, body=_BLOCKED_BODY)
        assert self._plan_path(_FAKE_SOLO_URL).read_text(encoding="utf-8") == _BLOCKED_BODY


class TestStructuredManifestRenderIsNotGated(TestCase):
    """A manifest's `**Blocked:** <reason>` disclosure is the honest mechanism and must post.

    The body gate scans verbatim free-text (the `--body-file` path); the structured
    `--manifest` render is NOT gated. The `blocked_workflows` feature renders a literal
    `**Blocked:** <reason>` line — a body scan would falsely refuse every honest
    colleague test-plan note that discloses a not-yet-deployed workflow.
    """

    @pytest.fixture(autouse=True)
    def _inject(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        self._monkeypatch = monkeypatch
        self._tmp = tmp_path
        disable_on_behalf_gate(tmp_path_factory, monkeypatch)
        import teatree.core.evidence.test_plan_blocked_gate as _gate  # noqa: PLC0415

        monkeypatch.setattr(_gate, "get_effective_settings", _fake_settings)

    def test_blocked_workflows_manifest_lands_in_the_colleague_ticket_plan(self) -> None:
        checkout = self._tmp / "checkout"
        _seed(_FAKE_COLLEAGUE_URL, checkout)
        self._monkeypatch.setattr(_write, "_resolve_worktree_or_none", lambda: None)
        manifest = json.dumps(
            {
                "ticket": "123",
                "workflows": [{"workflow": "Login"}],
                "local": {"commits": {"client": "aabb"}},
                "blocked_workflows": {"Login": "deploy blocked on cred"},
            }
        )

        from django.core.management import call_command  # noqa: PLC0415

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast(
                "dict[str, object]",
                call_command("e2e", "write-test-plan", ticket=_FAKE_COLLEAGUE_URL, manifest=manifest),
            )

        assert result["action"] == "created"
        plan = checkout / "test-plans" / "main-app-123.md"
        assert "**Blocked:** deploy blocked on cred" in plan.read_text(encoding="utf-8")
