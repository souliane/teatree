from unittest.mock import patch

import pytest

from teatree.backends.gitlab import api as gitlab_api


def test_current_username_returns_username(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"username": "adrien"},
    )

    result = client.current_username()

    assert result == "adrien"


def test_current_username_returns_empty_when_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.current_username()

    assert result == ""


def test_gitlab_api_reads_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "env-token")

    client = gitlab_api.GitLabAPI()

    assert client.token == "env-token"


def test_gitlab_api_explicit_token_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "env-token")

    client = gitlab_api.GitLabAPI(token="explicit-token")

    assert client.token == "explicit-token"


def test_resolve_token_falls_back_to_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``_resolve_token`` resolves through ``GitLabTokenCredential`` (the shared
    # credential machinery), which reads ``pass`` via the ``read_pass`` name bound
    # in ``teatree.llm.credentials`` — the canonical mock point that machinery's
    # own tests use (``tests/test_credential_config.py``).
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with patch("teatree.llm.credentials.read_pass", return_value="pass-token"):
        assert gitlab_api._resolve_token() == "pass-token"


def test_resolve_token_returns_empty_when_pass_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    with patch("teatree.llm.credentials.read_pass", return_value=""):
        assert gitlab_api._resolve_token() == ""


def test_resolve_token_prefers_env_over_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "env-token")
    assert gitlab_api._resolve_token() == "env-token"


class TestGitLabAPICacheHits:
    """Verify cache-hit branches for all cached methods."""

    def test_get_work_item_status_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        graphql_calls: list[int] = []
        monkeypatch.setattr(
            client,
            "graphql",
            lambda q, v: (
                graphql_calls.append(1)
                or {
                    "data": {
                        "project": {
                            "workItems": {
                                "nodes": [{"widgets": [{"type": "STATUS", "status": {"name": "In progress"}}]}],
                            },
                        },
                    },
                }
            ),
        )
        assert client.get_work_item_status("org/repo", 1) == "In progress"
        assert client.get_work_item_status("org/repo", 1) == "In progress"
        assert len(graphql_calls) == 1

    def test_get_mr_pipeline_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(
            client,
            "get_json",
            lambda ep: calls.append(1) or [{"status": "success", "web_url": "https://ci/1"}],
        )
        client.get_mr_pipeline(1, 1)
        result = client.get_mr_pipeline(1, 1)
        assert result["status"] == "success"
        assert len(calls) == 1

    def test_get_mr_approvals_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(
            client,
            "get_json",
            lambda ep: calls.append(1) or {"approved_by": [], "approvals_required": 1},
        )
        client.get_mr_approvals(1, 1)
        result = client.get_mr_approvals(1, 1)
        assert result["count"] == 0
        assert len(calls) == 1

    def test_get_issue_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(client, "get_json", lambda ep: calls.append(1) or {"iid": 5, "title": "Bug"})
        client.get_issue(1, 5)
        result = client.get_issue(1, 5)
        assert result is not None
        assert result["title"] == "Bug"
        assert len(calls) == 1

    def test_get_mr_discussions_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(client, "get_json_paginated", lambda ep: calls.append(1) or [{"id": "d1"}])
        client.get_mr_discussions(1, 1)
        result = client.get_mr_discussions(1, 1)
        assert result == [{"id": "d1"}]
        assert len(calls) == 1

    def test_get_draft_notes_count_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        # get_draft_notes_count paginates (F8.7): the cached count comes off get_json_paginated.
        monkeypatch.setattr(client, "get_json_paginated", lambda ep: calls.append(1) or [{"id": 1}, {"id": 2}])
        client.get_draft_notes_count(1, 1)
        result = client.get_draft_notes_count(1, 1)
        assert result == 2
        assert len(calls) == 1

    def test_current_username_returns_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(client, "get_json", lambda ep: calls.append(1) or {"username": "dev"})
        client.current_username()
        result = client.current_username()
        assert result == "dev"
        assert len(calls) == 1

    def test_clear_response_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = gitlab_api.GitLabAPI(token="t")
        calls = []
        monkeypatch.setattr(client, "get_json", lambda ep: calls.append(1) or {"username": "dev"})
        client.current_username()
        client.clear_response_cache()
        client.current_username()
        assert len(calls) == 2
