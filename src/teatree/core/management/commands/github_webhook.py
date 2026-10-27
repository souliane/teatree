from typing import IO, Annotated, cast

import typer
from django_typer.management import command

from teatree.backends.github.repository_webhooks import WebhookAccessError
from teatree.core.machine_output import MachineOutputCommand, emit
from teatree.core.management.commands._github_webhook_connect import (
    ConnectResult,
    WebhookConnectError,
    WebhookConnection,
    validate_target,
)
from teatree.forge_credentials import ForgeTokenState, resolve_slug_token
from teatree.utils.run import CommandFailedError, TimeoutExpired
from teatree.utils.secrets import SecretStoreError


class Command(MachineOutputCommand):
    """Create or remove a signed GitHub repository webhook."""

    def _connection(self, repo: str, url: str) -> WebhookConnection:
        validate_target(repo, url)
        resolution = resolve_slug_token(repo, forge="github", credential="github_token")
        if resolution.state is not ForgeTokenState.TOKEN or not resolution.token:
            message = f"{resolution.setting} is {resolution.state.value}; refusing ambient gh auth"
            raise WebhookConnectError(message)
        return WebhookConnection(repo, url, resolution.token, echo=self.stderr.write)

    def _emit(self, result: ConnectResult, *, json_output: bool) -> ConnectResult:
        self.print_result = False
        emit(
            result,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"{result['repo']}: hook {result['hook_id']} {result['status']}; "
            f"ping {'verified' if result['ping_verified'] else 'not checked'}. "
            "Webhook transport is not selected; selecting it is a separate step.",
        )
        return result

    def _run(self, action: str, repo: str, url: str, *, replace_secret: bool, json_output: bool) -> ConnectResult:
        try:
            connection = self._connection(repo, url)
            result = (
                connection.connect(replace_secret=replace_secret) if action == "connect" else connection.disconnect()
            )
        except TimeoutExpired as exc:
            self.stderr.write(f"GitHub webhook request timed out after {exc.timeout} seconds")
            raise SystemExit(1) from exc
        except (WebhookConnectError, WebhookAccessError, SecretStoreError, CommandFailedError) as exc:
            self.stderr.write(str(exc))
            raise SystemExit(1) from exc
        return self._emit(result, json_output=json_output)

    @command()
    def connect(
        self,
        *,
        repo: Annotated[str, typer.Option("--repo", help="Repository owner/name.")] = "",
        url: Annotated[str, typer.Option("--url", help="Public HTTPS URL for /hooks/github/.")] = "",
        replace_secret: Annotated[bool, typer.Option("--replace-secret", help="Rotate the signing secret.")] = False,
        disconnect: Annotated[
            bool, typer.Option("--disconnect", help="Remove the exact-URL hook and its secret.")
        ] = False,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON on stdout.")] = False,
    ) -> ConnectResult:
        if disconnect and replace_secret:
            self.stderr.write("--disconnect and --replace-secret cannot be combined")
            raise SystemExit(1)
        return self._run(
            "disconnect" if disconnect else "connect", repo, url, replace_secret=replace_secret, json_output=json_output
        )

    @command()
    def disconnect(
        self,
        *,
        repo: Annotated[str, typer.Option("--repo", help="Repository owner/name.")] = "",
        url: Annotated[str, typer.Option("--url", help="Exact webhook URL to remove.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON on stdout.")] = False,
    ) -> ConnectResult:
        return self._run("disconnect", repo, url, replace_secret=False, json_output=json_output)
