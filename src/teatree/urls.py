from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("", include("teatree.core.urls", namespace="teatree")),
]

if settings.DEBUG:
    urlpatterns.append(path("admin/", admin.site.urls))
