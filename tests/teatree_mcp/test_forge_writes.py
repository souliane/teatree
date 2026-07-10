"""Behaviour tests for the wave-2 forge WRITE MCP tools (#3076 item 3).

Each ``<forge>_issue_*`` write tool rides an existing
:class:`~teatree.core.backend_protocols.CodeHostBackend` method, resolved
through the same ``_forge_client`` (``code_host_from_overlay``) seam the reads
use, and routes every outbound body through the public-repo leak scrub
(``privacy_gate.scan_outbound_text``) + the #117 send-proxy chokepoint
(``send_proxy.route_send``) BEFORE the backend call. A scripted fake keeps the
tools hermetic — no ``gh``/``glab`` binary, no network — while proving the
forwarding, the per-service registration, and the banned-term refusal.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from asgiref.sync import async_to_sync
from django.test import TestCase

from teatree.backends.types import Service
from teatree.core.overlay import OverlayConfig
from teatree.mcp import build_server


class _ServiceOverlay:
    def __init__(self, *services: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))


class _FakeForge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_issue(self, *, repo: str, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        self.calls.append(("create_issue", {"repo": repo, "title": title, "body": body, "labels": labels}))
        return {"number": 5, "html_url": f"https://github.com/{repo}/issues/5"}

    def post_issue_comment(self, *, issue_url: str, body: str) -> dict[str, Any]:
        self.calls.append(("post_issue_comment", {"issue_url": issue_url, "body": body}))
        return {"id": 11}

    def close_issue(self, *, issue_url: str, comment: str = "") -> dict[str, Any]:
        self.calls.append(("close_issue", {"issue_url": issue_url, "comment": comment}))
        return {"state": "closed"}

    def update_issue(self, *, issue_url: str, body: str) -> dict[str, Any]:
        self.calls.append(("update_issue", {"issue_url": issue_url, "body": body}))
        return {"body": body}

    def repo_for_issue_url(self, issue_url: str) -> str:
        parts = issue_url.split("/")
        return "/".join(parts[3:5]) if len(parts) >= 5 else ""


@contextmanager
def _forge_env(fake: _FakeForge, *, service: Service = Service.GITHUB, public: bool = False) -> Iterator[None]:
    with (
        patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay(service)}),
        patch("teatree.mcp.services_forge._forge_client", return_value=fake),
        patch("teatree.core.gates.privacy_gate._target_is_public", return_value=public),
    ):
        yield


def _call(tool: str, args: dict[str, Any]) -> Any:
    result = async_to_sync(build_server().call_tool)(tool, args)
    structured = result[1] if isinstance(result, tuple) else result
    return structured["result"] if isinstance(structured, dict) and set(structured) == {"result"} else structured


class TestForgeIssueWriteTools(TestCase):
    def test_issue_create_forwards_and_returns_payload(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake):
            result = _call("github_issue_create", {"repo": "acme/widgets", "title": "bug", "body": "it breaks"})

        assert result["number"] == 5
        assert fake.calls[0] == (
            "create_issue",
            {"repo": "acme/widgets", "title": "bug", "body": "it breaks", "labels": None},
        )

    def test_issue_comment_forwards_to_the_backend(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake):
            _call(
                "github_issue_comment",
                {"issue_url": "https://github.com/acme/widgets/issues/7", "body": "still repro"},
            )

        assert fake.calls[0] == (
            "post_issue_comment",
            {"issue_url": "https://github.com/acme/widgets/issues/7", "body": "still repro"},
        )

    def test_issue_close_forwards_with_comment(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake):
            _call(
                "github_issue_close",
                {"issue_url": "https://github.com/acme/widgets/issues/7", "comment": "fixed in main"},
            )

        assert fake.calls[0] == (
            "close_issue",
            {"issue_url": "https://github.com/acme/widgets/issues/7", "comment": "fixed in main"},
        )

    def test_issue_update_forwards_the_new_body(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake):
            _call(
                "github_issue_update",
                {"issue_url": "https://github.com/acme/widgets/issues/7", "body": "revised description"},
            )

        assert fake.calls[0] == (
            "update_issue",
            {"issue_url": "https://github.com/acme/widgets/issues/7", "body": "revised description"},
        )

    def test_gitlab_prefix_registers_its_own_write_group(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake, service=Service.GITLAB):
            _call("gitlab_issue_create", {"repo": "acme/widgets", "title": "bug", "body": "it breaks"})

        assert fake.calls[0][0] == "create_issue"

    def test_issue_close_without_a_comment_skips_the_scrub(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake):
            _call("github_issue_close", {"issue_url": "https://github.com/acme/widgets/issues/7"})

        assert fake.calls[0] == (
            "close_issue",
            {"issue_url": "https://github.com/acme/widgets/issues/7", "comment": ""},
        )


class TestForgeWriteSendProxyRefusal(TestCase):
    def test_send_proxy_refusal_stops_the_write(self) -> None:
        # The leak scan passes (private target), but the #117 send-proxy refuses
        # the destination (enforce mode) ⇒ the write never reaches the backend.
        fake = _FakeForge()
        refused = SimpleNamespace(allowed=False, reason="send-proxy refused the destination", payload="")
        with (
            _forge_env(fake),
            patch("teatree.mcp.services_forge.route_send", return_value=refused),
            pytest.raises(Exception, match="send-proxy refused"),
        ):
            _call("github_issue_create", {"repo": "acme/widgets", "title": "t", "body": "b"})

        assert fake.calls == []


class TestForgeWriteScrub(TestCase):
    def test_banned_term_write_to_a_public_repo_is_refused_before_the_backend(self) -> None:
        # The public-repo leak scrub must fire BEFORE the backend call: a
        # customer codename bound for a public forge is refused, and nothing
        # reaches create_issue.
        fake = _FakeForge()
        with (
            _forge_env(fake, public=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
            pytest.raises(Exception, match="privacy gate refused"),
        ):
            _call(
                "github_issue_create",
                {"repo": "souliane/teatree", "title": "ship it", "body": "roll out for Contoso now"},
            )

        assert fake.calls == []

    def test_clean_body_to_a_public_repo_passes_the_scrub(self) -> None:
        fake = _FakeForge()
        with (
            _forge_env(fake, public=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            _call(
                "github_issue_create",
                {"repo": "souliane/teatree", "title": "ship it", "body": "roll out the generic feature"},
            )

        assert fake.calls[0][0] == "create_issue"

    def test_banned_term_in_a_label_to_a_public_repo_is_refused_before_the_backend(self) -> None:
        # A label is agent-controllable free text bound for a PUBLIC forge, and
        # GitHub auto-creates a non-existent label — so a customer codename in a
        # label must be leak-scrubbed exactly like the body: refused before the
        # backend, nothing reaches create_issue.
        fake = _FakeForge()
        with (
            _forge_env(fake, public=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
            pytest.raises(Exception, match="privacy gate refused"),
        ):
            _call(
                "github_issue_create",
                {
                    "repo": "souliane/teatree",
                    "title": "ship it",
                    "body": "roll out the generic feature",
                    "labels": ["Contoso-migration"],
                },
            )

        assert fake.calls == []

    def test_clean_labels_to_a_public_repo_are_forwarded(self) -> None:
        fake = _FakeForge()
        with (
            _forge_env(fake, public=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            _call(
                "github_issue_create",
                {
                    "repo": "souliane/teatree",
                    "title": "ship it",
                    "body": "roll out the generic feature",
                    "labels": ["bug", "enhancement"],
                },
            )

        assert fake.calls[0][0] == "create_issue"
        assert fake.calls[0][1]["labels"] == ["bug", "enhancement"]


class TestForgeWriteFailClosed(TestCase):
    def test_undeclared_service_registers_no_write_tools(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay()}):
            names = {tool.name for tool in asyncio.run(build_server().list_tools())}

        assert "github_issue_create" not in names
        assert "gitlab_issue_create" not in names
        assert not {
            n for n in names if n.endswith(("_issue_create", "_issue_comment", "_issue_close", "_issue_update"))
        }
