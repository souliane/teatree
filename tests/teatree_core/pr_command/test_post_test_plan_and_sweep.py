from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from tests.teatree_core.conftest import CommandOverlay

from ._shared import _MOCK_OVERLAY


class TestPostEvidence(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    @pytest.fixture(autouse=True)
    def _no_on_behalf_gate(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Disable the on-behalf gate (#960) for transport-mechanics tests.

        ``post-test-plan`` is on-behalf-gated; the tests here exercise the
        code-host delegation, not the gate (its own suite lives in
        ``tests/teatree_core/test_pr_post_test_plan_on_behalf_gate.py``).
        """
        from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate  # noqa: PLC0415

        disable_on_behalf_gate(tmp_path_factory, monkeypatch)

    def test_delegates_to_code_host(self) -> None:
        """post-test-plan posts a PR comment via the code host."""
        host = MagicMock()
        host.list_pr_comments.return_value = []
        host.post_pr_comment.return_value = {"id": 55}
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "post-test-plan", "10", "--body", "All tests pass")

        assert result == {"id": 55}
        host.post_pr_comment.assert_called_once()
        call_kw = host.post_pr_comment.call_args
        assert call_kw.kwargs["pr_iid"] == 10
        assert "All tests pass" in call_kw.kwargs["body"]

    def test_returns_error_without_code_host(self) -> None:
        """post-test-plan returns error when no code host configured."""
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: None)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "post-test-plan", "10")

        assert "error" in result


class TestSweep(TestCase):
    """``pr sweep`` lists all of the user's open PRs across the forge (#466)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_returns_open_prs_for_authenticated_user(self) -> None:
        from teatree.core.overlay import OverlayConfig  # noqa: PLC0415

        host = MagicMock()
        host.list_my_prs.return_value = [
            {
                "iid": 1,
                "title": "feat: x",
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/1",
                "source_branch": "feat-x",
                "target_branch": "main",
            },
            {
                "iid": 2,
                "title": "fix: y",
                "web_url": "https://gitlab.com/org/other/-/merge_requests/2",
                "source_branch": "fix-y",
                "target_branch": "develop",
            },
        ]
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)
        overlay = CommandOverlay()
        # Per-instance config so we don't mutate the class-level default shared by other tests.
        overlay.config = OverlayConfig()
        overlay.config.get_gitlab_username = lambda: "adrien"  # type: ignore[method-assign]

        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert result["author"] == "adrien"
        assert result["count"] == 2
        prs = cast("list[dict[str, object]]", result["prs"])
        assert prs[0]["target_branch"] == "main"
        assert prs[1]["target_branch"] == "develop"
        host.list_my_prs.assert_called_once_with(author="adrien")

    def test_falls_back_to_current_user_when_no_username_configured(self) -> None:
        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.list_my_prs.return_value = []
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert result["author"] == "souliane"
        assert result["count"] == 0
        host.list_my_prs.assert_called_once_with(author="souliane")

    def test_returns_error_when_no_code_host_configured(self) -> None:
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: None)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert "error" in result

    def test_returns_error_when_username_unresolved(self) -> None:
        host = MagicMock()
        host.current_user.return_value = ""
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert "error" in result
        host.list_my_prs.assert_not_called()
