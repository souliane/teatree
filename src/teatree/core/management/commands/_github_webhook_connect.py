import ipaddress
import re
from collections.abc import Callable
from datetime import timedelta
from math import ceil
from secrets import token_hex
from time import sleep
from typing import Self, TypedDict
from urllib.parse import urlsplit

from django.utils import timezone

from teatree.backends.github.repository_webhooks import RepositoryWebhooks, hook_body
from teatree.core.intake.github_payload import HOOK_EVENTS
from teatree.core.models import IncomingEvent
from teatree.core.views._webhook_secrets import REFRESH_WINDOW_SECONDS, STORE_PREFIX
from teatree.utils.secrets import read_pass, remove_pass, write_pass_with_backup

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class WebhookConnectError(RuntimeError):
    @classmethod
    def invalid_repo(cls) -> Self:
        return cls("--repo must be an owner/name repository slug")

    @classmethod
    def invalid_url(cls) -> Self:
        return cls("--url must be a public HTTPS URL ending in /hooks/github/ without credentials or query")

    @classmethod
    def missing_ping(cls) -> Self:
        return cls(
            "no ping delivery stored; check that the public URL reaches this listener with valid TLS "
            "and this command shares the listener's control DB"
        )

    @classmethod
    def hook_still_present(cls) -> Self:
        return cls("GitHub hook still appears after delete")


class ConnectResult(TypedDict):
    repo: str
    hook_id: int
    status: str
    ping_verified: bool


def webhook_target(repo_id: int) -> str:
    return f"repository-{repo_id}"


def secret_key(repo_id: int) -> str:
    return f"{STORE_PREFIX}/{webhook_target(repo_id)}"


def validate_target(repo: str, url: str) -> None:
    if not _REPO.fullmatch(repo) or ".." in repo:
        raise WebhookConnectError.invalid_repo()
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise WebhookConnectError.invalid_url() from exc
    scheme_and_path_valid = parsed.scheme == "https" and parsed.path == "/hooks/github/"
    authority_valid = bool(hostname) and parsed.username is None and parsed.password is None
    hostname_valid = hostname != "localhost" and not hostname.endswith((".localhost", ".local", ".internal"))
    if (
        not (scheme_and_path_valid and authority_valid and hostname_valid)
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise WebhookConnectError.invalid_url()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname:
            message = "--url must have a public DNS hostname"
            raise WebhookConnectError(message) from None
    else:
        if not address.is_global:
            message = "--url must have a public IP address"
            raise WebhookConnectError(message)


class WebhookConnection:
    def __init__(self, repo: str, url: str, token: str, *, echo: Callable[[str], object]) -> None:
        self.repo = repo
        self.url = url
        self.webhooks = RepositoryWebhooks(repo, token)
        self.echo = echo

    def connect(self, *, replace_secret: bool) -> ConnectResult:
        repo_id = self.webhooks.repository_id()
        hook = self.webhooks.find(self.url)
        key = secret_key(repo_id)
        stored = read_pass(key)
        secret = stored
        if not secret or replace_secret:
            secret = token_hex(32)
            write_pass_with_backup(key, secret, echo=self.echo)
        changed_secret = secret != stored
        if hook is None:
            hook = self.webhooks.create(hook_body(self.url, HOOK_EVENTS, secret))
            status = "created"
        elif changed_secret or not hook.satisfies(self.url, HOOK_EVENTS):
            self.webhooks.update(hook.id, hook_body(self.url, HOOK_EVENTS, secret))
            status = "updated"
        else:
            status = "unchanged"
        if status != "unchanged" and not self.webhooks.get(hook.id).satisfies(self.url, HOOK_EVENTS):
            message = "GitHub hook did not match after write"
            raise WebhookConnectError(message)
        repo_key = f"github:{repo_id}"
        started = timezone.now()
        polls_per_ping = 15
        attempts = ceil(REFRESH_WINDOW_SECONDS / polls_per_ping) + 1
        for attempt in range(attempts):
            self.echo(f"Checking GitHub ping delivery ({attempt + 1}/{attempts})")
            self.webhooks.ping(hook.id)
            for _ in range(polls_per_ping):
                elapsed = timezone.now() - started
                if (
                    IncomingEvent.objects.webhooks_verified_for(
                        "github", repo_key, "ping", elapsed + timedelta(seconds=1)
                    )
                    and IncomingEvent.objects.filter(
                        source="github",
                        repo_key=repo_key,
                        event_name="ping",
                        origin="webhook",
                        signer=webhook_target(repo_id),
                        received_at__gte=started,
                    ).exists()
                ):
                    return ConnectResult(repo=self.repo, hook_id=hook.id, status=status, ping_verified=True)
                sleep(1)
        raise WebhookConnectError.missing_ping()

    def disconnect(self) -> ConnectResult:
        repo_id = self.webhooks.repository_id()
        hook = self.webhooks.find(self.url)
        if hook is None:
            return ConnectResult(repo=self.repo, hook_id=0, status="absent", ping_verified=False)
        self.webhooks.delete(hook.id)
        if self.webhooks.find(self.url) is not None:
            raise WebhookConnectError.hook_still_present()
        key = secret_key(repo_id)
        if read_pass(key) and not remove_pass(key):
            message = f"GitHub hook removed, but secret {key} could not be removed"
            raise WebhookConnectError(message)
        return ConnectResult(repo=self.repo, hook_id=hook.id, status="disconnected", ping_verified=False)
