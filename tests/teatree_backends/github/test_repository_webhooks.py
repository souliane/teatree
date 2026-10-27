import pytest

from teatree.backends.github import repository_webhooks
from teatree.backends.github.repository_webhooks import (
    RepositoryHook,
    RepositoryWebhooks,
    WebhookAccessError,
    hook_body,
)


def test_hook_body_has_full_signed_configuration() -> None:
    body = hook_body("https://hooks.example.org/hooks/github/", ("push",), "canary")
    assert body == {
        "name": "web",
        "active": True,
        "events": ["push"],
        "config": {
            "url": "https://hooks.example.org/hooks/github/",
            "content_type": "json",
            "insecure_ssl": "0",
            "secret": "canary",
        },
    }


def test_hook_satisfies_detects_configuration_drift() -> None:
    url = "https://hooks.example.org/hooks/github/"
    data = {"id": 7, **hook_body(url, ("push",), "canary")}
    hook = RepositoryHook.from_api(data)
    assert hook.satisfies(url, ("push",))
    assert not hook.satisfies(url, ("pull_request",))
    assert not hook.satisfies("https://other.example.org/hooks/github/", ("push",))


@pytest.mark.parametrize(
    "data",
    [
        {"id": 7, "events": ["push"]},
        {"id": 7, "config": {}, "events": "push"},
        {"id": 7, "config": {}, "events": [1]},
        {"id": True, "config": {}, "events": []},
        {"id": 0, "config": {}, "events": []},
    ],
)
def test_malformed_hook_cannot_be_treated_as_absent(data: dict[str, object]) -> None:
    with pytest.raises(WebhookAccessError, match="invalid repository hook"):
        RepositoryHook.from_api(data)


@pytest.mark.parametrize("payload", [[], {}, {"id": True}, {"id": 0}, {"id": "7"}])
def test_repository_without_a_positive_integer_id_is_refused(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    monkeypatch.setattr(repository_webhooks, "gh_api_get", lambda *args, **kwargs: payload)
    with pytest.raises(WebhookAccessError, match="no repository id"):
        RepositoryWebhooks("sample/repo", "test-token").repository_id()


def test_non_object_hook_responses_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository_webhooks, "gh_api_get", lambda *args, **kwargs: [])
    monkeypatch.setattr(repository_webhooks, "_gh_api_post", lambda *args, **kwargs: None)
    webhooks = RepositoryWebhooks("sample/repo", "test-token")
    with pytest.raises(WebhookAccessError, match="invalid repository hook"):
        webhooks.get(7)
    with pytest.raises(WebhookAccessError, match="invalid repository hook"):
        webhooks.create({})


def test_duplicate_hooks_at_url_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://hooks.example.org/hooks/github/"
    hooks = [{"id": hook_id, **hook_body(url, ("push",), "canary")} for hook_id in (7, 8)]
    monkeypatch.setattr(repository_webhooks, "gh_api_get_paginated", lambda *args, **kwargs: hooks)
    with pytest.raises(WebhookAccessError, match="multiple"):
        RepositoryWebhooks("sample/repo", "test-token").find(url)
