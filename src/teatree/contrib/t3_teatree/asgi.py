import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.contrib.t3_teatree.settings")

application = get_asgi_application()
