"""Operator OAuth sign-in and user-scoped forge grants."""

import base64
import hashlib
import hmac
import secrets
import time
from datetime import timedelta
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import urlencode

import httpx
from django.contrib.auth import login
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseRedirect
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

from teatree.backends.forge_pool import client_for_url
from teatree.config.operator_oauth_apps import parse_operator_oauth_apps
from teatree.core.models import ConfigSetting, OnBehalfApproval, OnBehalfAudit, OperatorGrant, OperatorIdentity, Ticket
from teatree.core.models.operator_grant import OperatorGrantTokens
from teatree.core.models.operator_identity import identity_key
from teatree.utils.master_key import MasterKeySourceError, master_key_source
from teatree.utils.sealing import MasterKey
from teatree.utils.secrets import SecretStoreError, read_pass

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser
    from django.http import HttpRequest

_BACKEND = "django.contrib.auth.backends.ModelBackend"
_STATE_LIFETIME_SECONDS = 600
_REFRESH_SKEW = timedelta(minutes=1)
_TIMEOUT_SECONDS = 10.0


class _PendingLogin(TypedDict):
    forge: str
    state: str
    verifier: str
    expires: float
    next: str
    initiator: str
    purpose: str


class OperatorOAuthUnavailableError(RuntimeError):
    def __init__(self, forge: str) -> None:
        super().__init__(f"operator {forge} grant unavailable; run `t3 login {forge} --approvals`")


def _app(forge: str) -> dict[str, str]:
    raw = ConfigSetting.objects.get_effective("operator_oauth_apps")
    apps = parse_operator_oauth_apps(raw if raw is not None else {})
    if forge not in apps:
        raise OperatorOAuthUnavailableError(forge)
    return apps[forge]


def _base_url(forge: str, app: dict[str, str]) -> str:
    if forge == "github":
        return "https://github.com"
    return app.get("base_url", "https://gitlab.com").rstrip("/")


def _secret(app: dict[str, str], forge: str) -> str:
    value = read_pass(app["client_secret_pass_key"])
    if not value:
        raise OperatorOAuthUnavailableError(forge)
    return value


def _scope(forge: str, purpose: str) -> str:
    if forge == "github":
        return "read:user"
    return "read_user" if purpose == "login" else "api"


@require_GET
def start_login(request: "HttpRequest", forge: str) -> HttpResponse:
    return _start_authorization(request, forge, purpose="login")


@require_GET
def start_approval_consent(request: "HttpRequest", forge: str) -> HttpResponse:
    if not request.user.is_authenticated or not request.user.is_active or not request.user.is_superuser:
        return HttpResponseForbidden("sign in as an approved operator before granting approvals")
    return _start_authorization(request, forge, purpose="approvals")


def _start_authorization(request: "HttpRequest", forge: str, *, purpose: str) -> HttpResponse:
    if forge not in {"github", "gitlab"}:
        return HttpResponseBadRequest("unsupported forge")
    try:
        app = _app(forge)
    except (OperatorOAuthUnavailableError, ValueError, TypeError):
        return HttpResponse("operator OAuth app is unavailable; configure operator_oauth_apps", status=503)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    next_path = request.GET.get("next", "/admin/")
    if not url_has_allowed_host_and_scheme(
        next_path, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_path = "/admin/"
    request.session["operator_oauth_pending"] = {
        "forge": forge,
        "state": state,
        "verifier": verifier,
        "expires": time.time() + _STATE_LIFETIME_SECONDS,
        "next": next_path,
        "initiator": str(request.user.pk) if request.user.is_authenticated else "",
        "purpose": purpose,
    }
    params = {
        "client_id": app["client_id"],
        "redirect_uri": app["redirect_uri"],
        "response_type": "code",
        "scope": _scope(forge, purpose),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    route = "/login/oauth/authorize" if forge == "github" else "/oauth/authorize"
    return HttpResponseRedirect(f"{_base_url(forge, app)}{route}?{urlencode(params)}")


def _token_response(
    forge: str, app: dict[str, str], *, grant_type: str, credential: str, verifier: str = ""
) -> OperatorGrantTokens:
    data = {
        "client_id": app["client_id"],
        "client_secret": _secret(app, forge),
        "grant_type": grant_type,
    }
    if grant_type == "authorization_code":
        data.update({"code": credential, "code_verifier": verifier, "redirect_uri": app["redirect_uri"]})
    else:
        data["refresh_token"] = credential
    route = "/login/oauth/access_token" if forge == "github" else "/oauth/token"
    url = f"{_base_url(forge, app)}{route}"
    response = client_for_url(url).request(
        "POST",
        url,
        payload=urlencode(data),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        timeout=_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise OperatorOAuthUnavailableError(forge)
    now = timezone.now()
    refresh_seconds = payload.get("refresh_token_expires_in")
    expires_seconds = payload.get("expires_in")
    if expires_seconds is None and payload.get("refresh_token"):
        raise OperatorOAuthUnavailableError(forge)
    return OperatorGrantTokens(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token", "")),
        expires_at=now + timedelta(seconds=int(expires_seconds)) if expires_seconds is not None else now,
        refresh_expires_at=now + timedelta(seconds=int(refresh_seconds)) if refresh_seconds is not None else None,
        scope=str(payload.get("scope", "")),
    )


def _profile_response(forge: str, app: dict[str, str], access_token: str) -> httpx.Response:
    if forge == "github":
        url = "https://api.github.com/user"
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
    else:
        url = f"{_base_url(forge, app)}/api/v4/user"
        headers = {"Authorization": f"Bearer {access_token}"}
    return client_for_url(url).get(url, headers=headers, timeout=_TIMEOUT_SECONDS)


def _forge_identity(forge: str, app: dict[str, str], access_token: str) -> tuple[str, str]:
    response = _profile_response(forge, app, access_token)
    response.raise_for_status()
    profile = response.json()
    handle = profile.get("login" if forge == "github" else "username")
    external_id = profile.get("id")
    if not handle or external_id is None:
        raise OperatorOAuthUnavailableError(forge)
    return str(external_id), str(handle)


def _consume_pending(request: "HttpRequest", forge: str) -> _PendingLogin | None:
    pending = request.session.pop("operator_oauth_pending", None)
    state = request.GET.get("state", "")
    if not isinstance(pending, dict):
        return None
    initiator = str(request.user.pk) if request.user.is_authenticated else ""
    fresh = (
        pending.get("forge") == forge
        and state
        and state.isascii()
        and hmac.compare_digest(state, str(pending.get("state", "")))
        and time.time() <= float(pending.get("expires", 0))
        and pending.get("initiator", "") == initiator
    )
    return cast("_PendingLogin", pending) if fresh else None


def _admit_forge_account(forge: str, external_id: str, handle: str) -> "AbstractBaseUser | None":
    with transaction.atomic():
        identity = OperatorIdentity.objects.select_for_update().filter(platform=forge, external_id=external_id).first()
        if identity is None:
            identity = (
                OperatorIdentity.objects.select_for_update()
                .filter(platform=forge, handle=handle.casefold(), external_id="")
                .first()
            )
        if identity is None or not identity.user.is_active or not identity.user.is_superuser:
            return None
        old_key = identity.key
        new_key = identity_key(forge, handle)
        if old_key != new_key:
            Ticket.objects.filter(requested_by=old_key).update(requested_by=new_key)
            OnBehalfApproval.objects.filter(approver_id=old_key).update(approver_id=new_key)
            OnBehalfAudit.objects.filter(approver_id=old_key).update(approver_id=new_key)
        identity.handle = handle
        identity.external_id = external_id
        identity.save(update_fields=["handle", "external_id"])
        return identity.user


_LOGIN_FAILURES = (
    IntegrityError,
    MasterKeySourceError,
    SecretStoreError,
    OperatorOAuthUnavailableError,
    httpx.HTTPError,
    ValueError,
    KeyError,
    TypeError,
    OverflowError,
)


def _login_refusal(forge: str, exc: Exception, *, purpose: str) -> HttpResponse:
    if isinstance(exc, IntegrityError):
        return HttpResponseForbidden("forge handle already belongs to another approved account")
    if isinstance(exc, MasterKeySourceError):
        message = f"operator grants are sealed under the installation master key, which is unavailable ({exc}); "
        return HttpResponse(message + "create it as deploy/README.md describes", status=503)
    remedy = f"t3 login {forge}" if purpose == "login" else f"t3 login {forge} --approvals"
    return HttpResponse(f"operator OAuth login failed: {type(exc).__name__}; run `{remedy}`", status=502)


@require_GET
def finish_login(request: "HttpRequest", forge: str) -> HttpResponse:
    pending = _consume_pending(request, forge)
    if pending is None:
        return HttpResponseForbidden("operator OAuth state expired or invalid")
    code = request.GET.get("code", "")
    if not code:
        return HttpResponseBadRequest("operator OAuth code missing")
    try:
        app = _app(forge)
        tokens = _token_response(
            forge, app, grant_type="authorization_code", credential=code, verifier=pending["verifier"]
        )
        external_id, handle = _forge_identity(forge, app, tokens.access_token)
        user = _admit_forge_account(forge, external_id, handle)
    except _LOGIN_FAILURES as exc:
        return _login_refusal(forge, exc, purpose=pending["purpose"])
    if user is None:
        return HttpResponseForbidden("forge account needs administrator approval before sign-in")
    return _complete_authorization(request, forge, pending, tokens, user)


def _complete_authorization(
    request: "HttpRequest", forge: str, pending: _PendingLogin, tokens: OperatorGrantTokens, user: "AbstractBaseUser"
) -> HttpResponse:
    if pending.get("purpose") == "approvals":
        if not request.user.is_authenticated or request.user.pk != user.pk or not tokens.refresh_token:
            remedy = " (register a GitHub App with expiring user tokens)" if forge == "github" else ""
            return HttpResponseForbidden(
                f"approval consent must match the signed-in operator and include a refresh grant{remedy}"
            )
        try:
            OperatorGrant.objects.store(user=user, forge=forge, tokens=tokens, key=master_key_source().load())
        except _LOGIN_FAILURES as exc:
            return _login_refusal(forge, exc, purpose="approvals")
        return redirect(pending["next"])
    if pending.get("purpose") != "login":
        return HttpResponseForbidden("operator OAuth purpose invalid")
    login(request, user, backend=_BACKEND)
    return redirect(pending["next"])


def _fresh_grant(user: "AbstractBaseUser", forge: str) -> tuple[OperatorGrant, MasterKey]:
    grant = OperatorGrant.objects.filter(user=user, forge=forge).first()
    if grant is None:
        raise OperatorOAuthUnavailableError(forge)
    key = master_key_source().load()
    tokens = grant.reveal(key)
    if grant.expires_at > timezone.now() + _REFRESH_SKEW:
        return grant, key
    if grant.refresh_expires_at is not None and grant.refresh_expires_at <= timezone.now():
        raise OperatorOAuthUnavailableError(forge)
    try:
        refreshed = _token_response(forge, _app(forge), grant_type="refresh_token", credential=tokens.refresh_token)
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as exc:
        latest = OperatorGrant.objects.filter(pk=grant.pk).first()
        if latest is not None and bytes(latest.ciphertext) != bytes(grant.ciphertext):
            return latest, key
        raise OperatorOAuthUnavailableError(forge) from exc
    with transaction.atomic():
        current = OperatorGrant.objects.select_for_update().get(pk=grant.pk)
        if bytes(current.ciphertext) != bytes(grant.ciphertext):
            return current, key
        return OperatorGrant.objects.store(user=user, forge=forge, tokens=refreshed, key=key), key


def operator_access_token(
    user: "AbstractBaseUser",
    forge: str,
    *,
    action: str = "",
    target: str = "",
    approver_id: str = "",
) -> str:
    grant, key = _fresh_grant(user, forge)
    if action or target or approver_id:
        return grant.token_for(action, target, approver_id, key=key)
    return grant.reveal(key).access_token


def revoke_operator_grant(user: "AbstractBaseUser", forge: str) -> None:
    app = _app(forge)
    grant = OperatorGrant.objects.filter(user=user, forge=forge).first()
    if grant is None:
        raise OperatorOAuthUnavailableError(forge)
    access = operator_access_token(user, forge)
    grant.refresh_from_db()
    tokens = grant.reveal(master_key_source().load())
    secret = _secret(app, forge)
    if forge == "gitlab":
        for token in (access, tokens.refresh_token):
            url = f"{_base_url(forge, app)}/oauth/revoke"
            response = client_for_url(url).request(
                "POST",
                url,
                payload=urlencode({"client_id": app["client_id"], "client_secret": secret, "token": token}),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
    else:
        url = f"https://api.github.com/applications/{app['client_id']}/grant"
        credentials = base64.b64encode(f"{app['client_id']}:{secret}".encode()).decode()
        response = client_for_url(url).request(
            "DELETE",
            url,
            payload={"access_token": access},
            headers={"Authorization": f"Basic {credentials}", "Accept": "application/vnd.github+json"},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    verification = _profile_response(forge, app, access)
    if verification.status_code != httpx.codes.UNAUTHORIZED:
        message = f"operator {forge} grant is still active or could not be verified"
        raise RuntimeError(message)
    grant.delete()
