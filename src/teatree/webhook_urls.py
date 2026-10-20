from django.urls import path

from teatree.core.views import GitHubWebhookView, GitLabWebhookView

urlpatterns = [
    path("hooks/github/", GitHubWebhookView.as_view(), name="github_webhook"),
    path("hooks/gitlab/", GitLabWebhookView.as_view(), name="gitlab_webhook"),
]
