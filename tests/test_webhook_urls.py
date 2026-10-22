from django.urls import resolve

from teatree.webhook_urls import urlpatterns

_URLCONF = "teatree.webhook_urls"


def _receiver(path: str) -> str:
    view = resolve(path, urlconf=_URLCONF).func.view_class
    return f"{view.__module__}.{view.__qualname__}"


def test_listener_routes_each_hook_to_its_receiver() -> None:
    assert _receiver("/hooks/github/") == "teatree.core.views.github_webhook.GitHubWebhookView"
    assert _receiver("/hooks/gitlab/") == "teatree.core.views.gitlab_webhook.GitLabWebhookView"


def test_listener_has_exactly_the_two_hook_routes() -> None:
    assert {(str(route.pattern), route.name) for route in urlpatterns} == {
        ("hooks/github/", "github_webhook"),
        ("hooks/gitlab/", "gitlab_webhook"),
    }
