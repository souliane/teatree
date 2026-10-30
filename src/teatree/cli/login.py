"""Browser login and forge grant revocation for an operator."""

import webbrowser

import httpx
import typer

from teatree.backends.forge_pool import client_for_url
from teatree.cli.admin import _record_browse_url
from teatree.utils.django_bootstrap import ensure_django
from teatree.utils.loopback_forward import ensure_admin_forward
from teatree.utils.ports import running_in_container

_FORGES = frozenset({"github", "gitlab"})
_NATIVE_ADMIN_URL = "http://127.0.0.1:8000"


def _forge(value: str) -> str:
    if value not in _FORGES:
        typer.echo("Choose github or gitlab.", err=True)
        raise typer.Exit(code=2)
    return value


def login(
    forge: str = typer.Argument(help="GitHub or GitLab OAuth provider."),
    *,
    approvals: bool = typer.Option(False, "--approvals", help="Request a separate approval grant after sign-in."),
) -> None:
    provider = _forge(forge)
    path = f"/login/oauth/{provider}/{'approvals' if approvals else 'start'}/"
    if running_in_container():
        forward = ensure_admin_forward()
        if not forward.url:
            typer.echo(f"Cannot reach the admin: {forward.error}", err=True)
            raise typer.Exit(code=1)
        url = f"{forward.url}{path}"
        _record_browse_url(url)
    else:
        url = f"{_NATIVE_ADMIN_URL}{path}"
        probe_url = f"{_NATIVE_ADMIN_URL}/admin/login/" if approvals else url
        try:
            response = client_for_url(probe_url).get(probe_url, headers={}, timeout=5)
        except httpx.HTTPError as exc:
            typer.echo(f"Admin is not reachable; start `t3 admin`: {type(exc).__name__}", err=True)
            raise typer.Exit(code=1) from exc
        if not (response.is_success if approvals else response.is_redirect):
            typer.echo("Admin did not start OAuth; check the app configuration.", err=True)
            raise typer.Exit(code=1)
        webbrowser.open(url)
    typer.echo(f"Operator {'approval consent' if approvals else 'sign-in'}: {url}")


def logout(
    forge: str = typer.Argument(help="GitHub or GitLab OAuth provider."),
    *,
    user: str = typer.Option("", "--user", help="Operator username when several people have grants."),
) -> None:
    provider = _forge(forge)
    ensure_django()
    from teatree.core.models import OperatorGrant  # noqa: PLC0415 — Django setup precedes model import
    from teatree.core.operator_auth.operator_oauth import (  # noqa: PLC0415 — Django setup precedes model import
        revoke_operator_grant,
    )

    grants = OperatorGrant.objects.filter(forge=provider)
    if user:
        grant = grants.select_related("user").filter(user__username=user).first()
    else:
        if grants.count() > 1:
            typer.echo("Several operators have this grant; pass --user <username>.", err=True)
            raise typer.Exit(code=1)
        grant = grants.select_related("user").first()
    if grant is None:
        typer.echo(f"No {provider} grant for this operator; run `t3 login {provider} --approvals`.", err=True)
        raise typer.Exit(code=1)
    try:
        revoke_operator_grant(grant.user, provider)
    except (RuntimeError, ValueError, httpx.HTTPError) as exc:
        typer.echo(f"Could not revoke {provider} grant: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Revoked {provider} grant for {grant.user.username}.")
