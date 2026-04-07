"""ASGI entrypoint for E2E tests — uses e2e.settings."""

import os

os.environ["DJANGO_SETTINGS_MODULE"] = "e2e.settings"

from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # ty: ignore[unresolved-import]
from django.core.asgi import get_asgi_application

application = ASGIStaticFilesHandler(get_asgi_application())
