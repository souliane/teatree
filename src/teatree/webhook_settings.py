from typing import cast

from teatree import settings as base_settings

globals().update({name: value for name, value in vars(base_settings).items() if name.isupper()})

DEBUG = False
ROOT_URLCONF = "teatree.webhook_urls"
ALLOWED_HOSTS = ["*"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
INSTALLED_APPS = [
    app
    for app in base_settings.INSTALLED_APPS
    if app
    not in {
        "django.contrib.admin",
        "django.contrib.messages",
        "django.contrib.sessions",
        "django.contrib.staticfiles",
        "teatree.dash",
    }
]
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024  # GitHub's payload cap
# Inside GitHub's ~10 s delivery timeout, so a locked DB answers 503 instead of a dropped delivery.
DATABASES = {
    alias: config | {"OPTIONS": cast("dict[str, int | str]", config["OPTIONS"]) | {"timeout": 5}}
    for alias, config in base_settings.DATABASES.items()
}
