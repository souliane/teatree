"""Short-lived login links for an operator with shell access to this factory."""

from typing import TYPE_CHECKING, cast

from django.contrib.auth import get_user_model, login
from django.contrib.auth.models import AbstractBaseUser
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

if TYPE_CHECKING:
    from django.http import HttpRequest

_MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"
_LINK_LIFETIME_SECONDS = 120


def create_local_login_link(user: AbstractBaseUser, next_path: str) -> str:
    session = SessionStore()
    session["local_link_user_id"] = str(user.pk)
    session["local_link_next"] = next_path
    session.set_expiry(_LINK_LIFETIME_SECONDS)
    session.save()
    return f"/login/local/{cast('str', session.session_key)}/"


@require_GET
def consume_local_login_link(request: "HttpRequest", key: str) -> HttpResponse:
    row = Session.objects.filter(session_key=key, expire_date__gt=timezone.now()).first()
    if row is None:
        return HttpResponseForbidden("login link expired or already used")
    data = row.get_decoded()
    user_id = data.get("local_link_user_id")
    user = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    if user is None or not user.is_active or not user.is_superuser:
        return HttpResponseForbidden("login link cannot authenticate this user")
    deleted, _ = Session.objects.filter(pk=row.pk, expire_date__gt=timezone.now()).delete()
    if deleted != 1:
        return HttpResponseForbidden("login link expired or already used")
    login(request, user, backend=_MODEL_BACKEND)
    next_path = data.get("local_link_next", "/admin/")
    if not isinstance(next_path, str) or not url_has_allowed_host_and_scheme(
        next_path, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_path = "/admin/"
    return redirect(next_path)
