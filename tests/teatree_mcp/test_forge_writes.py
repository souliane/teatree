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
from teatree.core.overlay import OverlayConfig, OverlayConnectors
from teatree.mcp.server import build_server
from tests._send_gate import TEST_TERM_REGISTRY_JSON, allow_forge_repos
from tests.teatree_mcp._call_tool_result import structured as _structured


class _ServiceOverlay:
    def __init__(self, *services: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))
        self.connectors = OverlayConnectors()


class _FakeForge:
    def __init__(self, open_issues: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.description = "Original body."
        self.author = "souliane"
        self.open_issues = open_issues or []

    def list_repo_open_issues(self, *, repo: str) -> list[dict[str, Any]]:
        self.calls.append(("list_repo_open_issues", {"repo": repo}))
        return [dict(issue) for issue in self.open_issues]

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
        self.description = body
        return {"body": body}

    def repo_for_issue_url(self, issue_url: str) -> str:
        parts = issue_url.split("/")
        return "/".join(parts[3:5]) if len(parts) >= 5 else ""

    def get_issue(self, issue_url: str) -> dict[str, Any]:
        return {
            "html_url": issue_url,
            "body": self.description,
            "user": {"login": self.author},
            "updated_at": "2026-09-22T00:00:00Z",
        }

    def current_user(self) -> str:
        return "souliane"


@contextmanager
def _forge_env(fake: _FakeForge, *, service: Service = Service.GITHUB, public: bool = False) -> Iterator[None]:
    allow_forge_repos("acme/widgets", "souliane/teatree")
    with (
        patch.dict("os.environ", {"TEATREE_TERM_REGISTRY": TEST_TERM_REGISTRY_JSON}),
        patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay(service)}),
        patch("teatree.mcp.services_forge._forge_client", return_value=fake),
        patch("teatree.core.gates.privacy_gate._target_is_public", return_value=public),
    ):
        yield


#: An empty-backlog judgment: nothing to reject, so the create commits (#162 Rule 1).
_JUDGED_EMPTY: dict[str, Any] = {"snapshot": [], "decisions": []}


def _call(tool: str, args: dict[str, Any]) -> Any:
    result = async_to_sync(build_server().call_tool)(tool, args)
    return _structured(result)


def _writes(fake: "_FakeForge") -> list[tuple[str, dict[str, Any]]]:
    """The fake's MUTATIONS — the landscape read the facade always does is not one."""
    return [call for call in fake.calls if call[0] != "list_repo_open_issues"]


class TestForgeIssueWriteTools(TestCase):
    def test_issue_create_forwards_and_returns_payload(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake):
            result = _call(
                "github_issue_create",
                {"repo": "acme/widgets", "title": "bug", "body": "it breaks", "dedupe": _JUDGED_EMPTY},
            )

        assert result["outcome"] == "created_new"
        assert result["issue_url"] == "https://github.com/acme/widgets/issues/5"
        assert _writes(fake)[0][0] == "create_issue"
        assert _writes(fake)[0][1]["title"] == "bug"
        # The dedupe audit rides the body, so an empty backlog is recorded as one.
        assert "No open candidates existed" in _writes(fake)[0][1]["body"]

    def test_issue_note_appends_a_requirement_to_the_description(self) -> None:
        """A requirement in a comment is invisible to a lane, so #162 routes it to the body."""
        fake = _FakeForge()
        with _forge_env(fake):
            result = _call(
                "github_issue_note",
                {
                    "issue_url": "https://github.com/acme/widgets/issues/7",
                    "purpose": "requirement",
                    "body": "also handle the empty case",
                },
            )

        assert result["outcome"] == "appended"
        assert [name for name, _ in fake.calls] == ["update_issue"]
        assert "Original body." in fake.description
        assert "also handle the empty case" in fake.description

    def test_issue_note_posts_a_status_comment(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake):
            result = _call(
                "github_issue_note",
                {
                    "issue_url": "https://github.com/acme/widgets/issues/7",
                    "purpose": "status",
                    "body": "still repro",
                },
            )

        assert result["outcome"] == "commented"
        assert fake.calls[0] == (
            "post_issue_comment",
            {"issue_url": "https://github.com/acme/widgets/issues/7", "body": "still repro"},
        )

    def test_issue_note_refuses_every_comment_during_a_sweep(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake), pytest.raises(Exception, match="sweep posts no comments"):
            _call(
                "github_issue_note",
                {
                    "issue_url": "https://github.com/acme/widgets/issues/7",
                    "purpose": "evidence",
                    "body": "a screenshot",
                    "sweep_run_id": "run-1",
                },
            )
        assert fake.calls == []

    def test_issue_note_refuses_an_externally_authored_issue(self) -> None:
        fake = _FakeForge()
        fake.author = "someone.else"
        with _forge_env(fake), pytest.raises(Exception, match="refusing to modify"):
            _call(
                "github_issue_note",
                {
                    "issue_url": "https://github.com/acme/widgets/issues/7",
                    "purpose": "requirement",
                    "body": "do X",
                },
            )
        assert fake.calls == []

    def test_issue_close_puts_the_rationale_in_the_description_then_closes_with_no_comment(self) -> None:
        """#162: the reason a ticket was retired is normative text, so it goes in the body, not a comment."""
        fake = _FakeForge()
        with _forge_env(fake):
            result = _call(
                "github_issue_close",
                {"issue_url": "https://github.com/acme/widgets/issues/7", "rationale": "fixed in main"},
            )

        assert result["outcome"] == "closed"
        assert [name for name, _ in fake.calls] == ["update_issue", "close_issue"]
        assert "fixed in main" in fake.description
        assert fake.calls[1] == (
            "close_issue",
            {"issue_url": "https://github.com/acme/widgets/issues/7", "comment": ""},
        )

    def test_issue_close_refuses_an_externally_authored_issue(self) -> None:
        """Rule 5: a colleague's ticket is neither annotated nor closed — reported, not raised."""
        fake = _FakeForge()
        fake.author = "someone.else"
        with _forge_env(fake):
            result = _call(
                "github_issue_close",
                {"issue_url": "https://github.com/acme/widgets/issues/7", "rationale": "fixed in main"},
            )
        assert result["outcome"] == "external_refused"
        assert fake.calls == []

    def test_gitlab_prefix_registers_its_own_write_group(self) -> None:
        fake = _FakeForge()
        with _forge_env(fake, service=Service.GITLAB):
            _call(
                "gitlab_issue_create",
                {"repo": "acme/widgets", "title": "bug", "body": "it breaks", "dedupe": _JUDGED_EMPTY},
            )

        assert _writes(fake)[0][0] == "create_issue"


class TestIssueCreateDedupesFirst(TestCase):
    """#162 Rule 1 on the surface an agent actually files from.

    The inverse control is the one that matters: a call with no judgment set must
    WRITE NOTHING. A create that silently files when the dedupe is omitted is the
    pre-#162 behaviour wearing a new signature.
    """

    def _candidate(self, *, author: str = "souliane", title: str = "the empty case crashes") -> dict[str, Any]:
        return {
            "html_url": "https://github.com/acme/widgets/issues/3",
            "title": title,
            "body": "Original body.",
            "updated_at": "2026-09-20T00:00:00Z",
            "user": {"login": author},
        }

    def test_a_call_without_a_judgment_set_writes_nothing_and_returns_the_backlog(self) -> None:
        fake = _FakeForge([self._candidate()])
        with _forge_env(fake):
            result = _call("github_issue_create", {"repo": "acme/widgets", "title": "bug", "body": "b"})

        assert result["outcome"] == "judgment_required"
        assert result["snapshot"] == ["https://github.com/acme/widgets/issues/3"]
        assert result["candidates"][0]["title"] == "the empty case crashes"
        assert _writes(fake) == []

    def test_a_fitting_candidate_is_extended_instead_of_duplicated(self) -> None:
        fake = _FakeForge([self._candidate()])
        judged = {
            "snapshot": ["https://github.com/acme/widgets/issues/3"],
            "decisions": [{"url": "https://github.com/acme/widgets/issues/3", "fits": True}],
        }
        with _forge_env(fake):
            result = _call(
                "github_issue_create",
                {"repo": "acme/widgets", "title": "bug", "body": "also handle the empty case", "dedupe": judged},
            )

        assert result["outcome"] == "extended_existing"
        assert [name for name, _ in _writes(fake)] == ["update_issue"]
        assert "also handle the empty case" in fake.description

    def test_a_rejected_candidate_is_recorded_in_the_new_ticket_body(self) -> None:
        fake = _FakeForge([self._candidate()])
        judged = {
            "snapshot": ["https://github.com/acme/widgets/issues/3"],
            "decisions": [
                {"url": "https://github.com/acme/widgets/issues/3", "fits": False, "reason": "different subsystem"}
            ],
        }
        with _forge_env(fake):
            result = _call(
                "github_issue_create", {"repo": "acme/widgets", "title": "bug", "body": "b", "dedupe": judged}
            )

        assert result["outcome"] == "created_new"
        assert "different subsystem" in _writes(fake)[0][1]["body"]

    def test_an_unjudged_open_ticket_refuses_the_file(self) -> None:
        judged = {"snapshot": ["https://github.com/acme/widgets/issues/3"], "decisions": []}
        fake = _FakeForge([self._candidate()])
        with _forge_env(fake), pytest.raises(Exception, match="unjudged"):
            _call("github_issue_create", {"repo": "acme/widgets", "title": "bug", "body": "b", "dedupe": judged})

        assert _writes(fake) == []

    def test_a_ticket_filed_since_the_snapshot_reopens_the_judgment(self) -> None:
        """The race the snapshot exists for: judging a backlog that has since grown files nothing."""
        fake = _FakeForge([self._candidate()])
        with _forge_env(fake):
            result = _call(
                "github_issue_create",
                {"repo": "acme/widgets", "title": "bug", "body": "b", "dedupe": {"snapshot": [], "decisions": []}},
            )

        assert result["outcome"] == "stale_snapshot"
        assert result["unjudged"] == ["https://github.com/acme/widgets/issues/3"]
        assert _writes(fake) == []

    def test_a_fitting_ticket_someone_else_filed_is_neither_edited_nor_duplicated(self) -> None:
        fake = _FakeForge([self._candidate(author="someone.else")])
        # The re-read immediately before the write is the authority, not the candidate row.
        fake.author = "someone.else"
        judged = {
            "snapshot": ["https://github.com/acme/widgets/issues/3"],
            "decisions": [{"url": "https://github.com/acme/widgets/issues/3", "fits": True}],
        }
        with _forge_env(fake):
            result = _call(
                "github_issue_create", {"repo": "acme/widgets", "title": "bug", "body": "b", "dedupe": judged}
            )

        assert result["outcome"] == "external_conflict"
        assert _writes(fake) == []


class TestForgeWriteSendProxyRefusal(TestCase):
    def test_send_proxy_refusal_stops_the_write(self) -> None:
        # The leak scan passes (private target), but the #117 send-proxy refuses
        # the destination ⇒ the write never reaches the backend.
        fake = _FakeForge()
        refused = SimpleNamespace(allowed=False, reason="send-proxy refused the destination", payload="")
        with (
            _forge_env(fake),
            patch("teatree.core.send_proxy.route_send", return_value=refused),
            pytest.raises(Exception, match="send-proxy refused"),
        ):
            _call("github_issue_create", {"repo": "acme/widgets", "title": "t", "body": "b", "dedupe": _JUDGED_EMPTY})

        assert _writes(fake) == []


class TestForgeWriteScrub(TestCase):
    def test_banned_term_write_to_a_public_repo_is_refused_before_the_backend(self) -> None:
        # The public-repo leak scrub must fire BEFORE the backend call: a
        # customer codename bound for a public forge is refused, and nothing
        # reaches create_issue.
        fake = _FakeForge()
        with (
            _forge_env(fake, public=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["Contoso"], [])),
            pytest.raises(Exception, match="privacy gate refused"),
        ):
            _call(
                "github_issue_create",
                {
                    "repo": "souliane/teatree",
                    "title": "ship it",
                    "body": "roll out for Contoso now",
                    "dedupe": _JUDGED_EMPTY,
                },
            )

        assert _writes(fake) == []

    def test_clean_body_to_a_public_repo_passes_the_scrub(self) -> None:
        fake = _FakeForge()
        with (
            _forge_env(fake, public=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            _call(
                "github_issue_create",
                {
                    "repo": "souliane/teatree",
                    "title": "ship it",
                    "body": "roll out the generic feature",
                    "dedupe": _JUDGED_EMPTY,
                },
            )

        assert _writes(fake)[0][0] == "create_issue"

    def test_banned_term_in_a_label_to_a_public_repo_is_refused_before_the_backend(self) -> None:
        # A label is agent-controllable free text bound for a PUBLIC forge, and
        # GitHub auto-creates a non-existent label — so a customer codename in a
        # label must be leak-scrubbed exactly like the body: refused before the
        # backend, nothing reaches create_issue.
        fake = _FakeForge()
        with (
            _forge_env(fake, public=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["Contoso"], [])),
            pytest.raises(Exception, match="privacy gate refused"),
        ):
            _call(
                "github_issue_create",
                {
                    "repo": "souliane/teatree",
                    "title": "ship it",
                    "body": "roll out the generic feature",
                    "labels": ["Contoso-migration"],
                    "dedupe": _JUDGED_EMPTY,
                },
            )

        assert _writes(fake) == []

    def test_clean_labels_to_a_public_repo_are_forwarded(self) -> None:
        fake = _FakeForge()
        with (
            _forge_env(fake, public=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            _call(
                "github_issue_create",
                {
                    "repo": "souliane/teatree",
                    "title": "ship it",
                    "body": "roll out the generic feature",
                    "labels": ["bug", "enhancement"],
                    "dedupe": _JUDGED_EMPTY,
                },
            )

        assert _writes(fake)[0][0] == "create_issue"
        assert _writes(fake)[0][1]["labels"] == ["bug", "enhancement"]


class TestForgeWriteFailClosed(TestCase):
    def test_undeclared_service_registers_no_write_tools(self) -> None:
        with patch("teatree.mcp.server.get_all_overlays", return_value={"a": _ServiceOverlay()}):
            names = {tool.name for tool in asyncio.run(build_server().list_tools())}

        assert "github_issue_create" not in names
        assert "gitlab_issue_create" not in names
        assert not {n for n in names if n.endswith(("_issue_create", "_issue_note", "_issue_close"))}
