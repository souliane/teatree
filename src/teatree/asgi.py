"""ASGI entrypoint for the teatree dashboard."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")

from django.conf import settings
from django.core.asgi import get_asgi_application

application = get_asgi_application()

if settings.DEBUG:
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # ty: ignore[unresolved-import]

    application = ASGIStaticFilesHandler(application)
