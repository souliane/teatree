"""Shared plumbing for the ``t3 notion`` command groups.

Split out so the read/section surface and the page-level write surface each stay
one readable module while sharing exactly one credential resolution, one id
normalizer and one failure-to-exit-code mapping. A second copy of any of those is
how two commands start disagreeing about what exit 6 means.
"""

import dataclasses
from typing import TYPE_CHECKING

import typer

from teatree.utils.django_bootstrap import ensure_django

if TYPE_CHECKING:  # pragma: no cover — import-time cost stays off the CLI startup path
    from teatree.backends.notion.client import NotionClient
    from teatree.backends.notion.liveness import LivenessVerdict


def notion_client(overlay: str = "", *, version: str = "") -> "NotionClient":
    """Build a token-authenticated client; the token is read at point of use only."""
    ensure_django()
    from teatree.backends.notion.credentials import build_notion_client  # noqa: PLC0415 — deferred: lazy CLI import

    return build_notion_client(overlay or None, version=version)


def object_id(reference: str) -> str:
    from teatree.backends.notion.errors import normalize_object_id  # noqa: PLC0415 — deferred: lazy CLI import

    return normalize_object_id(reference)


def fail(exc: Exception) -> typer.Exit:
    """Print the diagnostic and exit with the condition's own code."""
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    typer.echo(str(exc), err=True)
    return typer.Exit(code=exc.exit_code if isinstance(exc, NotionError) else 1)


@dataclasses.dataclass(frozen=True, slots=True)
class LivePage:
    """A page id cleared for use, plus the audit stamp when it was NOT cleared."""

    page_id: str
    stamp: str = ""


def live_page(client: "NotionClient", reference: str, *, audit_reason: str = "") -> LivePage:
    """Normalize *reference*, and refuse it unless the page is provably the live version.

    The single chokepoint every page-scoped command on this surface goes through,
    so "is this page still the current one?" is asked once and answered the same
    way everywhere. A dead page renders as a completely current one, so a warning
    buried in the output is no control at all — the read exits 14 instead.

    ``audit_reason`` is the deliberate, on-the-record escape: it takes written
    prose (blank does not unblock), announces itself on stderr, and stamps the
    document that comes back, so an audited body cannot travel without saying
    what it is.
    """
    page_id = object_id(reference)
    verdict = client.page_liveness(page_id)
    if verdict.readable:
        return LivePage(page_id=page_id)
    if not audit_reason.strip():
        raise verdict.as_error(page_id)
    stamp = _audit_stamp(page_id, verdict, audit_reason.strip())
    typer.echo(stamp, err=True)
    return LivePage(page_id=page_id, stamp=stamp)


def _audit_stamp(page_id: str, verdict: "LivenessVerdict", reason: str) -> str:
    return (
        "!! ARCHIVED-PAGE AUDIT READ — what follows is NOT a current source.\n"
        f"!! {verdict.headline(page_id)}\n"
        f"!! {verdict.recovery()}\n"
        f"!! reason given: {reason}\n\n"
    )
