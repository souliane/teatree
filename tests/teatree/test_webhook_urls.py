import pytest
from django.urls import Resolver404, resolve

_URLCONF = "teatree.webhook_urls"


def _receiver(path: str) -> str:
    view = resolve(path, urlconf=_URLCONF).func.view_class
    return f"{view.__module__}.{view.__qualname__}"


def test_listener_routes_each_hook_to_its_receiver() -> None:
    assert _receiver("/hooks/github/") == "teatree.core.views.github_webhook.GitHubWebhookView"
    assert _receiver("/hooks/gitlab/") == "teatree.core.views.gitlab_webhook.GitLabWebhookView"


@pytest.mark.parametrize("path", ["/admin/", "/dash/", "/"])
def test_listener_resolves_nothing_else(path: str) -> None:
    with pytest.raises(Resolver404):
        resolve(path, urlconf=_URLCONF)
