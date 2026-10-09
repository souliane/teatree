"""WSGI entry point for the teatree Django project.

``t3 admin`` serves ``teatree.wsgi:application`` under gunicorn (a production
WSGI server), so the admin no longer depends on Django's dev ``runserver``.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")

application = get_wsgi_application()
