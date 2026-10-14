from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.types import Service
from teatree.core.backend_protocols import PrMessage
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp.server import build_server
from tests._send_gate import allow_forge_repos
from tests.teatree_mcp._call_tool_result import structured


class _Overlay:
    def __init__(self) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset({Service.GITHUB}))
        self.connectors = OverlayConnectors()


class _Forge:
    def __init__(self) -> None:
        self.message = PrMessage("Old title", "Old body")
        self.author = "owner"
        self.updates: list[PrMessage] = []

    def current_user(self) -> str:
        return "owner"

    def get_pr_author(self, *, pr_url: str) -> str:
        return self.author

    def fetch_pr_message(self, *, slug: str, pr_id: int) -> PrMessage:
        return self.message

    def update_pr_message(self, *, slug: str, pr_id: int, message: PrMessage) -> PrMessage:
        self.updates.append(message)
        self.message = message
        return message


def _call(forge: _Forge, expected_body: str = "Old body") -> object:
    allow_forge_repos("acme/widgets")
    with (
        patch("teatree.mcp.server.get_all_overlays", return_value={"a": _Overlay()}),
        patch("teatree.mcp.services_forge._forge_client", return_value=forge),
        patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
    ):
        return structured(
            async_to_sync(build_server().call_tool)(
                "github_pr_update",
                {
                    "repo": "acme/widgets",
                    "pr_iid": 7,
                    "expected": {"title": "Old title", "body": expected_body},
                    "message": {"title": "New title", "body": "New body"},
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
