"""Read forge push events for bot-owned repositories and report human actors."""

import json
import os
from pathlib import Path
from urllib.parse import quote

import typer

from teatree.cli.update import _collect_repos
from teatree.core.forge_push import forge_write_env
from teatree.hooks import bot_push_gate
from teatree.hooks.bot_push_gate import BotRepo
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail

_PAGE_SIZE = 100
_MAX_PAGES = 3
_READ_TIMEOUT_SECONDS = 15


def _event_argv(remote: BotRepo, page: int) -> list[str]:
    if remote.forge == "github":
        endpoint = f"repos/{remote.repo_path}/events?per_page={_PAGE_SIZE}&page={page}"
        return ["gh", "api", endpoint, "--hostname", remote.host]
    project = quote(remote.repo_path, safe="")
    endpoint = f"projects/{project}/events?action=pushed&per_page={_PAGE_SIZE}&page={page}"
    return ["glab", "api", endpoint, "--hostname", remote.host]


def _read_events(remote: BotRepo, path: Path) -> list[RawAPIDict]:
    routed = forge_write_env(str(path))
    key = "GH_TOKEN" if remote.forge == "github" else "GITLAB_TOKEN"
    if not routed.get(key):
        message = "no routed bot credential"
        raise RuntimeError(message)
    events: list[RawAPIDict] = []
    for page in range(1, _MAX_PAGES + 1):
        try:
            result = run_allowed_to_fail(
                _event_argv(remote, page),
                expected_codes=None,
                env={**os.environ, **routed},
                timeout=_READ_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutExpired) as exc:
            message = "forge events request failed"
            raise RuntimeError(message) from exc
        if result.returncode != 0:
            message = "forge events request failed"
            raise RuntimeError(message)
        try:
            page_events = json.loads(result.stdout)
        except ValueError as exc:
            message = "forge events were not JSON"
            raise RuntimeError(message) from exc
        if not isinstance(page_events, list) or not all(isinstance(event, dict) for event in page_events):
            message = "forge events were not an array of events"
            raise RuntimeError(message)
        events.extend(page_events)
        if len(page_events) < _PAGE_SIZE:
            return events
    message = "forge events exceeded the bounded three-page read"
    raise RuntimeError(message)


def _push_actor(remote: BotRepo, event: RawAPIDict) -> str | None:
    if remote.forge == "github":
        if event.get("type") != "PushEvent":
            return None
        actor = event.get("actor")
        login = actor.get("login") if isinstance(actor, dict) else None
    else:
        if event.get("action_name") != "pushed" and not isinstance(event.get("push_data"), dict):
            return None
        author = event.get("author")
        login = author.get("username") if isinstance(author, dict) else event.get("author_username")
    return login.strip() if isinstance(login, str) else ""


def check_bot_push_events() -> bool:
    seen: set[str] = set()
    healthy = True
    for _, path in _collect_repos():
        remote_url = git.remote_url(repo=str(path))
        try:
            remote = bot_push_gate.bot_repo_for(remote_url)
        except bot_push_gate.BotIdentityUnreadableError:
            typer.echo("FAIL  Bot push events could not be checked: bot identity configuration could not be read")
            return False
        if remote is None or remote.slug in seen:
            continue
        seen.add(remote.slug)
        try:
            events = _read_events(remote, path)
        except RuntimeError as exc:
            typer.echo(f"FAIL  Bot push events for {remote.slug} could not be read: {exc}")
            healthy = False
            continue
        for event in events:
            actor = _push_actor(remote, event)
            if actor is not None and actor.casefold() != remote.login:
                typer.echo(f"FAIL  {remote.slug} has a push event by {actor or '<unknown>'} (event {event.get('id')})")
                healthy = False
    return healthy
