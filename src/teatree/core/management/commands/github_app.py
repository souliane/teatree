"""``t3 <overlay> github_app`` — GitHub App manifest/registration/installation admin (#4795).

Mirrors ``config_setting.py``'s shape: a ``django_typer`` ``TyperCommand`` whose
subcommands are the operator surface for App creation, the manifest-code
callback, per-installation repo confirmation, delivery/preset status, and the
fail-closed ``webhook`` preset switch. Every subcommand prints only non-secret
data — the App's private key, webhook secret, and client secret never appear
in a subcommand's output (they are written straight to ``pass`` by
:mod:`teatree.backends.github.app_registration`).
"""

import datetime as dt
import json
from typing import Annotated, NoReturn

import httpx
import typer
from django_typer.management import TyperCommand, command

from teatree.backends.github.app_manifest import build_manifest
from teatree.backends.github.app_registration import exchange_manifest_code
from teatree.config import GitHubTransportPreset, get_effective_settings
from teatree.core.models import ConfigSetting, GitHubAppInstallation, WebhookRejection

#: How recent a verified delivery must be before ``set-preset webhook`` is
#: allowed — the fail-closed gate the #4795 AC requires.
_WEBHOOK_CUTOVER_WINDOW = dt.timedelta(hours=24)


class Command(TyperCommand):
    def _refuse(self, message: str) -> NoReturn:
        self.stderr.write(f"  refusing: {message}")
        raise SystemExit(2)

    @command()
    def manifest(
        self,
        name: Annotated[str, typer.Option("--name", help="The App's display name.")],
        url: Annotated[str, typer.Option("--url", help="The App's homepage URL.")],
        webhook_url: Annotated[str, typer.Option("--webhook-url", help="Where GitHub delivers webhooks.")],
    ) -> None:
        """Print a deterministic App-creation manifest as JSON — no secrets, reviewable."""
        manifest = build_manifest(name=name, url=url, webhook_url=webhook_url)
        self.stdout.write(json.dumps(manifest, indent=2, sort_keys=True))

    @command()
    def register(
        self, code: Annotated[str, typer.Argument(help="The manifest-flow code GitHub redirected with.")]
    ) -> None:
        """Exchange *code* for App credentials; persist secrets to ``pass``, print identity only."""
        try:
            registered = exchange_manifest_code(code)
        except httpx.HTTPStatusError as exc:
            self._refuse(f"manifest-code exchange failed: HTTP {exc.response.status_code}")
        self.stdout.write(
            f"  registered app_id={registered.app_id} slug={registered.slug!r} "
            f"name={registered.name!r} html_url={registered.html_url}"
        )

    @command(name="confirm-repos")
    def confirm_repos(
        self,
        installation_id: Annotated[int, typer.Argument(help="The GitHub installation id.")],
        repo: Annotated[
            list[str] | None, typer.Option("--repo", help="A repo to confirm; omit to confirm every pending repo.")
        ] = None,
    ) -> None:
        """Move pending repositories to active for one installation — the anti-broadening confirm."""
        installation = GitHubAppInstallation.objects.filter(installation_id=installation_id).first()
        if installation is None:
            self._refuse(f"no installation {installation_id} on file")
        moved = installation.confirm_repositories(repo)
        if not moved:
            self.stdout.write("  nothing to confirm (no matching pending repos)")
            return
        self.stdout.write(f"  confirmed: {', '.join(moved)}")

    @command()
    def status(self) -> None:
        """Delivery health, backlog, and the active transport preset — the metrics AC surface."""
        preset = get_effective_settings().github_transport_preset
        self.stdout.write(f"  active preset: {preset.value}")
        for installation in GitHubAppInstallation.objects.all():
            state = "suspended" if installation.is_suspended else "active"
            verified = installation.last_verified_delivery_at or "never"
            self.stdout.write(
                f"  installation {installation.installation_id} ({installation.account_login}, {state}): "
                f"active_repos={len(installation.active_repositories)} "
                f"pending_repos={len(installation.pending_repositories)} last_verified_delivery={verified}"
            )
        recent_rejections = WebhookRejection.objects.filter(source="github")[:20]
        self.stdout.write(f"  recent rejections: {recent_rejections.count()}")
        for rejection in recent_rejections:
            self.stdout.write(f"    {rejection.occurred_at.isoformat()} {rejection.reason}")

    @command(name="set-preset")
    def set_preset(self, preset: Annotated[str, typer.Argument(help="polling | webhook")]) -> None:
        """Switch the transport preset — ``webhook`` fails closed with no recent verified delivery."""
        try:
            parsed = GitHubTransportPreset.parse(preset)
        except ValueError as exc:
            self._refuse(str(exc))
        if parsed is GitHubTransportPreset.WEBHOOK:
            verified = GitHubAppInstallation.objects.filter(suspended_at__isnull=True).exclude(
                last_verified_delivery_at__isnull=True
            )
            has_recent = any(row.has_recent_verified_delivery(within=_WEBHOOK_CUTOVER_WINDOW) for row in verified)
            if not has_recent:
                self._refuse(
                    "no installation has a verified delivery within the last "
                    f"{_WEBHOOK_CUTOVER_WINDOW} — validate the webhook first (preset stays 'polling')"
                )
        ConfigSetting.objects.set_value("github_transport_preset", parsed.value)
        self.stdout.write(f"  github_transport_preset = {parsed.value}")
