from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.types import Service
from teatree.core.backend_protocols import PrMergeState, PrMessage
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp.server import build_server
from tests._send_gate import allow_forge_repos
from tests.teatree_mcp._call_tool_result import structured

_NEW_MESSAGE = PrMessage("New title", "New body")
_AI_VENDOR = "anthropic"
_AI_SIGNED_MESSAGE = PrMessage("New title", f"New body\n\nCo-Authored-By: Claude <noreply@{_AI_VENDOR}.com>")


class _Metadata:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.calls: list[tuple[str, str, bool, str]] = []

    def validate_pr(self, title: str, description: str, *, require_sections: bool = True, repo: str = "") -> dict:
        self.calls.append((title, description, require_sections, repo))
        return {"errors": self.errors, "warnings": []}


class _Overlay:
    def __init__(self) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset({Service.GITHUB}))
        self.connectors = OverlayConnectors()
        self.metadata = _Metadata()


class _Forge:
    def __init__(self) -> None:
        self.message = PrMessage("Old title", "Old body")
        self.author = "owner"
        self.state = "OPEN"
        self.updates: list[PrMessage] = []

    def current_user(self) -> str:
        return "owner"

    def get_pr_author(self, *, pr_url: str) -> str:
        return self.author

    def fetch_pr_message(self, *, slug: str, pr_id: int) -> PrMessage:
        return self.message

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        return PrMergeState(state=self.state, merge_commit_oid="")

    def update_pr_message(self, *, slug: str, pr_id: int, message: PrMessage) -> PrMessage:
        self.updates.append(message)
        self.message = message
        return message


def _call(
    forge: _Forge,
    expected_body: str = "Old body",
    *,
    message: PrMessage = _NEW_MESSAGE,
    overlay: _Overlay | None = None,
    owner: str = "",
) -> object:
    allow_forge_repos("acme/widgets")
    overlays = {"a": overlay or _Overlay()}
    with (
        patch("teatree.mcp.server.get_all_overlays", return_value=overlays),
        patch("teatree.mcp.services_forge.get_all_overlays", return_value=overlays),
        patch("teatree.mcp.services_forge._forge_client", return_value=forge),
        patch("teatree.mcp.services_forge.owning_overlay_for_repo", return_value=owner),
        patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
    ):
        return structured(
            async_to_sync(build_server().call_tool)(
                "github_pr_update",
                {
                    "repo": "acme/widgets",
                    "pr_iid": 7,
                    "expected": {"title": "Old title", "body": expected_body},
                    "message": {"title": message.title, "body": message.body},
                },
            )
        )


class TestPrUpdate(TestCase):
    def test_own_pr_update_checks_old_text_then_verifies_new_text(self) -> None:
        forge = _Forge()

        result = _call(forge)

        assert result == {"updated": True, "title": "New title", "body": "New body"}
        assert forge.updates == [PrMessage("New title", "New body")]

    def test_stale_or_foreign_pr_is_refused_without_write(self) -> None:
        forge = _Forge()
        with pytest.raises(ToolError, match="changed"):
            _call(forge, expected_body="stale")
        forge.author = "colleague"
        with pytest.raises(ToolError, match="own PR"):
            _call(forge)
        assert forge.updates == []

    def test_merged_or_closed_pr_is_refused_without_write(self) -> None:
        forge = _Forge()
        for state in ("MERGED", "CLOSED"):
            forge.state = state
            with self.subTest(state=state), pytest.raises(ToolError, match="open"):
                _call(forge)
        assert forge.updates == []

    def test_ai_signature_trailer_is_refused_without_write(self) -> None:
        forge = _Forge()

        with pytest.raises(ToolError, match="AI-signature"):
            _call(forge, message=_AI_SIGNED_MESSAGE)

        assert forge.updates == []

    def test_owning_overlay_validator_failure_is_refused_without_write(self) -> None:
        forge, overlay = _Forge(), _Overlay()
        overlay.metadata.errors = ["title must match the convention"]

        with pytest.raises(ToolError, match="convention"):
            _call(forge, overlay=overlay, owner="a")

        assert forge.updates == []
        assert overlay.metadata.calls == [("New title", "New body", True, "acme/widgets")]

    def test_title_only_update_skips_required_sections(self) -> None:
        forge, overlay = _Forge(), _Overlay()

        _call(forge, overlay=overlay, owner="a", message=PrMessage("New title", "Old body"))

        assert overlay.metadata.calls == [("New title", "Old body", False, "acme/widgets")]
        assert forge.updates == [PrMessage("New title", "Old body")]
