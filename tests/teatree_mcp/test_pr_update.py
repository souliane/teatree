from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp.server.mcpserver.exceptions import ToolError

from teatree.backends.types import Service
from teatree.core.backend_protocols import PrMergeState, PrMessage, PrOpenState
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
    def __init__(self, service: Service = Service.GITHUB, *, owner: str = "", public: bool = False) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset({service}))
        self.connectors = OverlayConnectors()
        self.metadata = _Metadata()
        self.owner = owner
        self.public = public


class _Forge:
    def __init__(self) -> None:
        self.message = PrMessage("Old title", "Old body")
        self.author = "owner"
        self.state = "OPEN"
        self.updates: list[PrMessage] = []
        self.last_pr_url = ""

    def current_user(self) -> str:
        return "owner"

    def get_pr_author(self, *, pr_url: str) -> str:
        self.last_pr_url = pr_url
        return self.author

    def fetch_pr_message(self, *, slug: str, pr_id: int) -> PrMessage:
        return self.message

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        return PrMergeState(state=self.state, merge_commit_oid="")

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return PrOpenState.OPEN if self.state in {"OPEN", "OPENED"} else PrOpenState.CLOSED

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
) -> object:
    allow_forge_repos("acme/widgets")
    contributor = overlay or _Overlay()
    service = next(iter(contributor.config.required_third_party_services))
    overlays = {"a": contributor}
    with (
        patch("teatree.mcp.server.get_all_overlays", return_value=overlays),
        patch("teatree.mcp.services_forge.get_all_overlays", return_value=overlays),
        patch("teatree.mcp.services_forge._forge_client", return_value=forge),
        patch("teatree.mcp.services_forge.owning_overlay_for_repo", return_value=contributor.owner),
        patch("teatree.core.gates.privacy_gate._target_is_public", return_value=contributor.public),
    ):
        return structured(
            async_to_sync(build_server().call_tool)(
                f"{service.value}_pr_update",
                {
                    "repo": "acme/widgets",
                    "pr_iid": 7,
                    "expected": {"title": "Old title", "body": expected_body},
                    "message": {"title": message.title, "body": message.body},
                },
            )
        )


class TestPrUpdate(TestCase):
    def test_gitlab_opened_mr_can_be_updated(self) -> None:
        forge = _Forge()
        forge.state = "OPENED"
        assert _call(forge, overlay=_Overlay(Service.GITLAB)) == {
            "updated": True,
            "title": "New title",
            "body": "New body",
        }
        assert forge.updates == [_NEW_MESSAGE]

    def test_gitlab_self_hosted_url_is_used_for_author_and_audit(self) -> None:
        forge = _Forge()
        forge.state = "OPENED"
        overlay = _Overlay(Service.GITLAB, owner="a")
        overlay.config.gitlab_url = "https://git.example.test/api/v4"
        _call(forge, overlay=overlay)
        assert forge.last_pr_url == "https://git.example.test/acme/widgets/-/merge_requests/7"

    def test_public_repo_leak_is_refused_before_update(self) -> None:
        forge = _Forge()
        with (
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["SECRETCORP"], [])),
            pytest.raises(ToolError, match="privacy gate refused"),
        ):
            _call(forge, message=PrMessage("New title", "SECRETCORP rollout"), overlay=_Overlay(public=True))
        assert forge.updates == []

    def test_backend_refusal_is_a_readable_tool_error(self) -> None:
        forge = _Forge()
        with (
            patch.object(forge, "update_pr_message", side_effect=RuntimeError("did not retain")),
            pytest.raises(ToolError, match="did not retain"),
        ):
            _call(forge)

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
        overlay.owner = "a"

        with pytest.raises(ToolError, match="convention"):
            _call(forge, overlay=overlay)

        assert forge.updates == []
        assert overlay.metadata.calls == [("New title", "New body", True, "acme/widgets")]

    def test_title_only_update_skips_required_sections(self) -> None:
        forge, overlay = _Forge(), _Overlay()

        overlay.owner = "a"
        _call(forge, overlay=overlay, message=PrMessage("New title", "Old body"))

        assert overlay.metadata.calls == [("New title", "Old body", False, "acme/widgets")]
        assert forge.updates == [PrMessage("New title", "Old body")]
