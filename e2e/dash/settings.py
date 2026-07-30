"""Django settings for the /dash/ e2e lane.

Derives from ``teatree.settings`` — the production config, so staticfiles +
WhiteNoise + the admin/dash apps are all wired — then forces DEBUG on (so
``live_server``'s staticfiles finder serves the vendored CSS/JS/fonts without a
collectstatic step) and a clean in-memory SQLite (no WAL/IMMEDIATE options, which
are meaningless for an in-memory live-server DB). Only UPPERCASE names (Django's
setting convention) are copied, so there is no ``import *`` and nothing suppressed.
"""

import teatree.settings as _base

globals().update({name: value for name, value in vars(_base).items() if name.isupper()})

DEBUG = True
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}
# ``live_server`` serves /static/ from the staticfiles finders directly; WhiteNoise
# is redundant here and warns about the uncollected STATIC_ROOT, which the suite's
# ``filterwarnings=error`` would turn into a hard error.
MIDDLEWARE = [middleware for middleware in _base.MIDDLEWARE if "whitenoise" not in middleware.lower()]
