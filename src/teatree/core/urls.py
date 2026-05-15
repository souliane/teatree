"""URL config for the `teatree.core` app.

The HTML dashboard was removed in #541; the statusline-driven harness
makes user-facing URLs unnecessary. The only routes exposed are
inbound webhook receivers from external platforms (#654).
"""

from django.urls import URLPattern, URLResolver, path

from teatree.core.views import GitHubWebhookView, GitLabWebhookView, SlackWebhookView

app_name = "teatree"

urlpatterns: list[URLPattern | URLResolver] = [
    path("hooks/slack/", SlackWebhookView.as_view(), name="slack_webhook"),
    path("hooks/gitlab/", GitLabWebhookView.as_view(), name="gitlab_webhook"),
    path("hooks/github/", GitHubWebhookView.as_view(), name="github_webhook"),
]
