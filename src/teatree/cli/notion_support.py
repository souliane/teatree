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
    from teatree.backends.notion.errors import NotionError
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
class VerdictLine:
    """One rendered triage line, and whether it belongs on stderr."""

    text: str
    err: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class PageVerdict:
    """Whether one page is reachable AND live for this integration, with its rendered lines."""

    page_id: str
    lines: tuple[VerdictLine, ...]
    error: "NotionError | None" = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def headline(self) -> str:
        """The whole verdict on ONE line, for a caller reporting a table of pages."""
        return "readable and live" if self.error is None else str(self.error)

    def echo(self) -> None:
        for line in self.lines:
            typer.echo(line.text, err=line.err)


def page_verdict(client: "NotionClient", reference: str) -> PageVerdict:
    """The reachable-and-live stages of the triage, rendered but never exited on.

    Shared by ``t3 notion doctor`` (which exits on the verdict) and
    ``t3 notion setup`` (which reports one line per page and keeps going), so the
    two surfaces cannot drift on what exit 6 means or on what a ``live:`` line says.
    """
    from teatree.backends.notion.errors import NotionError  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        page_id = object_id(reference)
        client.get_page(page_id)
    except NotionError as exc:
        return PageVerdict(reference, (VerdictLine(f"page:  FAIL — {exc}", err=True),), exc)
    lines = [VerdictLine("page:  OK — readable by this integration")]
    verdict = client.page_liveness(page_id)
    if verdict.readable:
        lines.append(VerdictLine(f"live:  OK — {verdict.detail}"))
        return PageVerdict(page_id, tuple(lines))
    lines.extend(
        (
            VerdictLine(f"live:  {verdict.state.value.upper()} — {verdict.detail}", err=True),
            VerdictLine(f"       {verdict.recovery()}", err=True),
        )
    )
    return PageVerdict(page_id, tuple(lines), verdict.as_error(page_id))


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
