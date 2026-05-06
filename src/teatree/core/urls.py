"""URL config for the `teatree.core` app.

The HTML dashboard was removed in #541; the statusline-driven harness
makes URLs unnecessary for the user-facing surface. This file is kept
so Django's URL resolver still treats `teatree.core` as a registered
app, but no routes are exposed.
"""

from django.urls import URLPattern, URLResolver

app_name = "teatree"

urlpatterns: list[URLPattern | URLResolver] = []
