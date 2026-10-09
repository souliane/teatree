"""Django settings for the teatree-self provisioning integration server.

Extends the project test settings so the booted ``runserver`` accepts the
loopback host the readiness probe targets. Used only by
``test_teatree_self.py`` when it spawns a real server subprocess.
"""

from tests.django_settings import *  # noqa: F403

ALLOWED_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
DEBUG = False
