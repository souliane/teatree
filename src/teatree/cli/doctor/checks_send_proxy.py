"""Check that overlays with outbound destinations can pass the send proxy."""

import typer
from django.core.exceptions import ImproperlyConfigured

from teatree import config
from teatree.config import cold_reader
from teatree.config.setting_parsers import _parse_private_repos
from teatree.core import overlay_loader
from teatree.core.send_proxy import SendChannel, channel_for_forge, destination_allowed, normalized_forge_repo
from teatree.utils.forge import forge_from_remote
from teatree.utils.git_remote import slug_from_remote


def _check_send_proxy_allowlist() -> bool:
    """Fail when destinations lack coverage in the global sender allowlist."""
    ok = True
    for entry in config.discover_overlays():
        try:
            overlay = overlay_loader.get_overlay(entry.name)
            repos = overlay.get_repos()
            _channel_name, channel_id = overlay.config.get_review_channel()
        except (ImportError, ImproperlyConfigured, RuntimeError, ValueError) as exc:
            typer.echo(f"FAIL  could not load overlay {entry.name!r} for send-proxy check: {exc}")
            ok = False
            continue
        if not repos and not channel_id:
            continue
        try:
            provisioning = getattr(overlay, "provisioning", None)
            destinations: list[tuple[SendChannel, str]] = []
            for repo in repos:
                remote = (provisioning.repo_clone_url(repo) or repo) if provisioning is not None else repo
                channel = channel_for_forge(forge_from_remote(remote) or "github")
                destinations.append((channel, normalized_forge_repo(slug_from_remote(remote))))
        except RuntimeError as exc:
            typer.echo(f"FAIL  invalid forge destination for overlay {entry.name!r}: {exc}")
            ok = False
            continue
        if channel_id:
            destinations.append((SendChannel.SLACK, channel_id))
        for channel, destination in destinations:
            if destination_allowed(channel, destination, overlay=""):
                continue
            typer.echo(
                f"FAIL  global send_proxy_allowlist does not cover {channel.value}:{destination} "
                f"for overlay {entry.name!r}. Set it with "
                '`t3 teatree config_setting set send_proxy_allowlist \'["github:owner/repo","slack:C123"]\'`.'
            )
            ok = False
    return ok


def _check_malformed_private_repos() -> bool:
    """List legacy entries that cannot protect a private destination."""
    stored = cold_reader.read_setting("private_repos")
    malformed: list[object] = [] if stored is None or isinstance(stored, list) else [stored]
    for entry in stored if isinstance(stored, list) else []:
        try:
            _parse_private_repos([entry])
        except (TypeError, ValueError):
            malformed.append(entry)
    if malformed:
        typer.echo(
            f"FAIL  malformed stored private_repos entries: {malformed!r}. "
            "Set host/owner or host/owner/repo entries with `t3 <overlay> config_setting set private_repos <value>`."
        )
        return False
    return True
