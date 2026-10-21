import os

from django.core.wsgi import get_wsgi_application

# The dedicated listener must ignore an inherited admin settings module.
os.environ["DJANGO_SETTINGS_MODULE"] = "teatree.webhook_settings"

application = get_wsgi_application()
