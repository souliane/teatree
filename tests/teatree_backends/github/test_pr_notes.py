"""GitHub review-thread mutation transport."""

import json
import subprocess
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from teatree.backends.forge_pool import close_forge_clients
from teatree.backends.github import api, pr_notes
from teatree.backends.github.pr_notes import GitHubPrNotes
from tests._forge_server import forge_server


@pytest.fixture(autouse=True)
def _fresh_pool() -> Iterator[None]:
    close_forge_clients()
    yield
    close_forge_clients()


def test_discussion_list_remains_empty_for_github_stale_bot_filter() -> None:
    assert GitHubPrNotes("tok").list_discussions(repo="o/r", pr_iid=9) == []


def test_resolve_thread_returns_confirmed_resolution() -> None:
    payload = {"data": {"resolveReviewThread": {"thread": {"id": "thread-7", "isResolved": True}}}}
    result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
    with patch.object(pr_notes, "_run_gh", return_value=result, create=True) as run:
        resolved = GitHubPrNotes("tok").resolve_thread(thread_id="thread-7")

    assert resolved == {"id": "thread-7", "isResolved": True}
    assert "resolveReviewThread" in str(run.call_args.args)


def test_unconfirmed_resolution_raises() -> None:
    result = subprocess.CompletedProcess([], 0, json.dumps({"data": {"resolveReviewThread": {"thread": None}}}), "")
    with (
        patch.object(pr_notes, "_run_gh", return_value=result, create=True),
        pytest.raises(RuntimeError, match="not confirmed"),
    ):
        GitHubPrNotes("tok").resolve_thread(thread_id="thread-7")


def test_resolve_thread_sends_the_thread_id_under_graphql_variables_on_the_wire() -> None:
    confirmed = {"data": {"resolveReviewThread": {"thread": {"id": "thread-7", "isResolved": True}}}}
    with (
        forge_server() as server,
        patch.object(api, "_api_url", lambda endpoint: f"{server.base}/{endpoint.lstrip('/')}"),
    ):
        server.payloads["/graphql"] = confirmed

        assert GitHubPrNotes("tok").resolve_thread(thread_id="thread-7")["id"] == "thread-7"
        body = json.loads(server.bodies[0])

    assert body["variables"] == {"threadId": "thread-7"}
    assert "threadId" not in body
    assert body["query"].startswith("mutation($threadId: ID!)")
