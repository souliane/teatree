from django.urls import include, path

urlpatterns = [
    path("", include("teatree.core.urls", namespace="teatree")),
]
