"""URL config for the `teatree.core` app.

The HTML dashboard was removed in #541; the statusline-driven harness
makes user-facing URLs unnecessary. The only routes exposed are
inbound webhook receivers from external platforms (#654 phase 1).
"""

from django.urls import URLPattern, URLResolver, path

from teatree.core.views import SlackWebhookView

app_name = "teatree"

urlpatterns: list[URLPattern | URLResolver] = [
    path("hooks/slack/", SlackWebhookView.as_view(), name="slack_webhook"),
]
