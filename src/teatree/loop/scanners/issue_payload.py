"""Readers of the forge issue payload that GitHub and GitLab spell differently."""

import datetime as dt
from typing import cast

from teatree.types import RawAPIDict


def issue_url(issue: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def issue_created_at(issue: RawAPIDict) -> dt.datetime | None:
    """When *issue* was FILED — the intake queue's ordering key on both forges.

    GitHub and GitLab both name the field ``created_at``. A naive timestamp is read as
    UTC, which is what both forges emit; an absent or unparsable one yields ``None``.
    """
    raw = issue.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def issue_author(issue: RawAPIDict) -> str:
    """The handle that FILED *issue*, across both forges' payload shapes.

    GitHub nests the author under ``user.login``; GitLab under ``author.username``.
    Returns ``""`` when no author can be resolved — which the gate reads as UNTRUSTED,
    never as a wildcard.
    """
    for container in ("user", "author"):
        value = issue.get(container)
        if not isinstance(value, dict):
            continue
        for name in ("login", "username"):
            handle = cast("RawAPIDict", value).get(name)
            if isinstance(handle, str) and handle.strip():
                return handle.strip()
    return ""
