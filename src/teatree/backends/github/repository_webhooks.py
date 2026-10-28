from dataclasses import dataclass
from typing import Self

from teatree.backends.github.api import (
    _gh_api_delete,
    _gh_api_empty,
    _gh_api_patch,
    _gh_api_post,
    gh_api_get,
    gh_api_get_paginated,
    is_not_found,
)
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError


class WebhookAccessError(RuntimeError):
    @classmethod
    def invalid_hook(cls) -> Self:
        return cls("GitHub returned an invalid repository hook")

    @classmethod
    def invalid_events(cls) -> Self:
        return cls("GitHub returned invalid repository hook events")

    @classmethod
    def invalid_list(cls) -> Self:
        return cls("GitHub returned an unreadable repository hook list")

    @classmethod
    def duplicate_url(cls) -> Self:
        return cls("GitHub returned multiple repository hooks at the same URL")

    @classmethod
    def no_repository_id(cls) -> Self:
        return cls("GitHub returned no repository id")

    @classmethod
    def permission(cls, operation: str) -> Self:
        return cls(
            f"GitHub could not {operation} repository hooks; grant repository admin access and the Webhooks permission"
        )


@dataclass(frozen=True, slots=True)
class RepositoryHook:
    id: int
    url: str
    events: frozenset[str]
    active: bool
    content_type: str
    insecure_ssl: str

    @classmethod
    def from_api(cls, data: RawAPIDict) -> Self:
        config = data.get("config")
        hook_id = data.get("id")
        if not isinstance(config, dict) or isinstance(hook_id, bool) or not isinstance(hook_id, int) or hook_id < 1:
            raise WebhookAccessError.invalid_hook()
        events = data.get("events")
        if not isinstance(events, list) or not all(isinstance(event, str) for event in events):
            raise WebhookAccessError.invalid_events()
        return cls(
            id=hook_id,
            url=str(config.get("url", "")),
            events=frozenset(events),
            active=data.get("active") is True,
            content_type=str(config.get("content_type", "")),
            insecure_ssl=str(config.get("insecure_ssl", "")),
        )

    def satisfies(self, url: str, events: tuple[str, ...]) -> bool:
        return (
            self.url == url
            and self.events == frozenset(events)
            and self.active
            and self.content_type == "json"
            and self.insecure_ssl == "0"
        )


def hook_body(url: str, events: tuple[str, ...], secret: str) -> RawAPIDict:
    return {
        "name": "web",
        "active": True,
        "events": list(events),
        "config": {"url": url, "content_type": "json", "insecure_ssl": "0", "secret": secret},
    }


class RepositoryWebhooks:
    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token
        self.endpoint = f"repos/{repo}/hooks"

    def repository_id(self) -> int:
        data = gh_api_get(f"repos/{self.repo}", token=self.token)
        if not isinstance(data, dict):
            raise WebhookAccessError.no_repository_id()
        repo_id = data.get("id")
        if isinstance(repo_id, bool) or not isinstance(repo_id, int) or repo_id < 1:
            raise WebhookAccessError.no_repository_id()
        return repo_id

    def find(self, url: str) -> RepositoryHook | None:
        try:
            data = gh_api_get_paginated(self.endpoint, token=self.token, strict=True)
        except ValueError as exc:
            raise WebhookAccessError.invalid_list() from exc
        except CommandFailedError as exc:
            if "HTTP 403" in exc.stderr or is_not_found(exc):
                operation = "list"
                raise WebhookAccessError.permission(operation) from exc
            raise
        hooks = [RepositoryHook.from_api(item) for item in data]
        matches = [hook for hook in hooks if hook.url == url]
        if len(matches) > 1:
            raise WebhookAccessError.duplicate_url()
        return matches[0] if matches else None

    def get(self, hook_id: int) -> RepositoryHook:
        data = gh_api_get(f"{self.endpoint}/{hook_id}", token=self.token)
        if not isinstance(data, dict):
            raise WebhookAccessError.invalid_hook()
        return RepositoryHook.from_api(data)

    def create(self, body: RawAPIDict) -> RepositoryHook:
        try:
            data = _gh_api_post(self.endpoint, body, token=self.token)
        except CommandFailedError as exc:
            if "HTTP 403" in exc.stderr or is_not_found(exc):
                operation = "create"
                raise WebhookAccessError.permission(operation) from exc
            raise
        if not isinstance(data, dict):
            raise WebhookAccessError.invalid_hook()
        return RepositoryHook.from_api(data)

    def update(self, hook_id: int, body: RawAPIDict) -> None:
        _gh_api_patch(f"{self.endpoint}/{hook_id}", body, token=self.token)

    def delete(self, hook_id: int) -> None:
        try:
            _gh_api_delete(f"{self.endpoint}/{hook_id}", token=self.token)
        except CommandFailedError as exc:
            if not is_not_found(exc):
                raise

    def ping(self, hook_id: int) -> None:
        _gh_api_empty(f"{self.endpoint}/{hook_id}/pings", method="POST", token=self.token)
