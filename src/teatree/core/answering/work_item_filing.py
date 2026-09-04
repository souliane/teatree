"""File the forge issue an owner request becomes, or refuse loudly (#4527).

The ``answering`` phase runs shell-denied, so its agent cannot file anything
itself — it hands a ``work_item`` back through the result envelope and this module
is the server-side half that acts on it, mirroring the ``article_suggestions`` and
``triage_recommendations`` channels.

The failure it exists to remove: the phase minted a ``Ticket`` with no
``issue_url``, announced it in Slack as "tracking as ticket N", and intake — which
discovers candidates from forge queries — could never see the row. Fifty of them
accumulated, each the only surviving record of one owner request. So the work item
is a REAL forge issue and its ``Ticket`` is
:meth:`~teatree.core.models.ticket.Ticket.is_admissible`; the conversation lane's
own row stays bookkeeping.

Every refusal is loud. A quiet ``None`` on a failed filing is indistinguishable
from "the agent decided nothing needed building", which is the silent drop this
whole change is about — only an explicit ``no_work_reason`` returns ``None``.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from teatree.core.intake.factory_admission import DEFAULT_ADMIT_LABEL
from teatree.core.models import NEEDS_TRIAGE_LABEL, Ticket
from teatree.core.models.types import SlackAnswerContext
from teatree.core.overlay_loader import get_overlay
from teatree.core.overlay_repos import owned_repo_slugs
from teatree.core.send_proxy import OutboundBlockedError, forge_from_url, route_forge_write
from teatree.types import RawAPIDict
from teatree.url_classify import find_forge_urls

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend

logger = logging.getLogger(__name__)

#: The dedupe marker embedded in every issue this filer creates. The DB-local stamp
#: on the conversation row is the first line of defence; this is the backstop for a
#: run whose stamp never landed (a crash between the forge write and the DB write).
FINGERPRINT_MARKER = "slack-request-fingerprint:"

_SLACK_ANSWER_KEY = "slack_answer"
_WORK_URL_KEY = "work_issue_url"
_ACTION = "answering_work_item"
_DESCRIPTION_LIMIT = 80


class WorkItemFilingError(RuntimeError):
    """The work item could not be filed and the caller must not read that as "nothing to do"."""


class EmptyWorkItemError(WorkItemFilingError):
    """The agent returned a ``work_item`` naming none of the three outcomes."""

    def __init__(self) -> None:
        super().__init__(
            "work_item names neither a title, an existing_issue_url, nor a no_work_reason — "
            "an empty envelope is indistinguishable from a dropped request"
        )


class UnresolvableIssueRefError(WorkItemFilingError):
    """The agent named an ``existing_issue_url`` that is not a forge issue URL."""

    def __init__(self, candidate: str) -> None:
        super().__init__(
            f"existing_issue_url {candidate!r} is not a forge issue URL — a reference nothing "
            "can resolve records a promise the owner cannot follow"
        )


class NoCodeHostError(WorkItemFilingError):
    """No code host is configured for the overlay, so the request cannot be filed anywhere."""

    def __init__(self, overlay: str) -> None:
        super().__init__(f"no code host is configured for overlay {overlay or '(default)'}")


class CodeHostUnresolvableError(WorkItemFilingError):
    """Building the overlay's code host raised, so the request cannot be filed anywhere."""

    def __init__(self, overlay: str, cause: Exception) -> None:
        super().__init__(f"the code host for overlay {overlay or '(default)'} could not be resolved: {cause}")


class NoFilingRepoError(WorkItemFilingError):
    """The overlay declares no repo, so there is nowhere the request could be filed."""

    def __init__(self, overlay: str) -> None:
        super().__init__(f"overlay {overlay or '(default)'} declares no repos, so there is nowhere to file")


class UntrackableIssueError(WorkItemFilingError):
    """The forge accepted the issue but returned no URL to track it by."""

    def __init__(self, repo: str) -> None:
        super().__init__(f"the forge accepted the issue for {repo} but returned no URL to track it by")


class ForgeRefusedError(WorkItemFilingError):
    """The forge rejected the create — a 403, a rate limit, a network fault, the posting gate."""

    def __init__(self, repo: str, cause: Exception) -> None:
        super().__init__(f"the forge refused the issue for {repo}: {cause}")


@dataclass(frozen=True, slots=True)
class FiledWorkItem:
    """Where an owner request ended up — or why it went nowhere."""

    url: str
    withheld: bool = False
    withheld_reason: str = ""


def file_work_item(
    ticket: Ticket,
    envelope: Mapping[str, object],
    *,
    host: "CodeHostBackend",
    repo: str,
) -> FiledWorkItem | None:
    """File (or attach to) the forge issue *envelope* names; ``None`` only for declared no-work."""
    if envelope.get("no_work_reason"):
        return None
    recorded = _recorded_work_url(ticket)
    if recorded:
        return FiledWorkItem(url=recorded)
    existing = str(envelope.get("existing_issue_url") or "").strip()
    if existing:
        return _attach(ticket, _validated_forge_url(existing))
    if not str(envelope.get("title") or "").strip():
        raise EmptyWorkItemError
    return _file_new(ticket, envelope, host=host, repo=repo)


def filing_repo(overlay: str) -> str:
    """The repo slug this overlay files owner requests into — its first owned repo.

    Raises rather than guessing: filing into a repo nobody declared would put the
    owner's request somewhere neither they nor intake is looking.
    """
    slugs = owned_repo_slugs(get_overlay(overlay or None))
    if not slugs:
        raise NoFilingRepoError(overlay)
    return slugs[0]


def _file_new(ticket: Ticket, envelope: Mapping[str, object], *, host: "CodeHostBackend", repo: str) -> FiledWorkItem:
    title = str(envelope.get("title") or "").strip()
    body = str(envelope.get("body") or "").strip()
    fingerprint = _fingerprint(ticket)
    already = _existing_by_fingerprint(host, repo=repo, fingerprint=fingerprint)
    if already:
        return _attach(ticket, already)
    stamped = f"{body}\n\n<!-- {_marker(fingerprint)} -->"
    forge = forge_from_url(f"https://github.com/{repo}")
    try:
        clean_title = route_forge_write(forge=forge, repo=repo, text=title, action=_ACTION, target=repo)
        clean_body = route_forge_write(forge=forge, repo=repo, text=stamped, action=_ACTION, target=repo)
    except OutboundBlockedError as exc:
        return FiledWorkItem(url="", withheld=True, withheld_reason=str(exc))
    try:
        raw = host.create_issue(repo=repo, title=clean_title, body=clean_body, labels=_labels())
    except Exception as exc:  # every transport fails differently, and any escape takes the reply with it
        raise ForgeRefusedError(repo, exc) from exc
    url = _issue_url(raw)
    if not url:
        raise UntrackableIssueError(repo)
    return _attach(ticket, url, title=clean_title)


def _labels() -> list[str]:
    """The admit label plus the maintainer gate every issue this filer opens must carry.

    Nobody dictated this text — it is the agent's paraphrase of a message a deliberately
    fail-open reader called work-implying — and the factory files as the maintainer's own
    account, so the author-keyed auto-triage Action cannot supply the gate. Without
    ``needs-triage`` the implementer scanner claims the issue before anyone reviews it.
    """
    return [DEFAULT_ADMIT_LABEL, NEEDS_TRIAGE_LABEL]


def _attach(ticket: Ticket, url: str, *, title: str = "") -> FiledWorkItem:
    """Mint (or reuse) the ADMISSIBLE work ticket for *url* and stamp the conversation row.

    The ticket is created here rather than left to the next forge poll so the work is
    claimable the moment the owner is told about it — the promise and the admissibility
    that backs it land together.
    """
    Ticket.objects.get_or_create(
        issue_url=url,
        defaults={
            "overlay": ticket.overlay,
            "role": Ticket.Role.AUTHOR,
            "short_description": (title or ticket.short_description)[:_DESCRIPTION_LIMIT],
        },
    )
    _record_work_url(ticket, url)
    return FiledWorkItem(url=url)


def _existing_by_fingerprint(host: "CodeHostBackend", *, repo: str, fingerprint: str) -> str:
    """An open issue already carrying this request's marker, or ``""``.

    Best-effort against forge search indexing — an issue filed seconds ago may not be
    indexed yet, so a back-to-back re-run could file once more. The DB-local stamp is
    what makes that rare; this catches the run whose stamp never landed.
    """
    try:
        matches = host.search_open_issues(repo=repo, query=_marker(fingerprint))
    except Exception as exc:  # noqa: BLE001 — a dedupe read failure must not lose the owner's request
        logger.warning("Work-item dedupe search failed for %s (%s); filing without it", repo, exc)
        return ""
    for raw in matches:
        if _marker(fingerprint) in str(raw.get("body") or raw.get("description") or ""):
            return _issue_url(raw)
    return ""


def _marker(fingerprint: str) -> str:
    """The ONE spelling of the dedupe token — the search query and the body check share it.

    A query that differs from the stored text by so much as a space finds nothing and
    the dedupe silently degrades to always-file.
    """
    return f"{FINGERPRINT_MARKER}{fingerprint}"


def _validated_forge_url(candidate: str) -> str:
    urls = find_forge_urls(candidate)
    if not urls:
        raise UnresolvableIssueRefError(candidate)
    return urls[0]


def _slack_answer(ticket: Ticket) -> SlackAnswerContext:
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    origin = extra.get(_SLACK_ANSWER_KEY)
    return cast("SlackAnswerContext", origin) if isinstance(origin, dict) else SlackAnswerContext()


def _fingerprint(ticket: Ticket) -> str:
    return str(_slack_answer(ticket).get("fingerprint") or "") or f"ticket-{ticket.pk}"


def _recorded_work_url(ticket: Ticket) -> str:
    return str(_slack_answer(ticket).get(_WORK_URL_KEY) or "")


def _record_work_url(ticket: Ticket, url: str) -> None:
    if _recorded_work_url(ticket) == url:
        return
    ticket.merge_extra(merge_into_dicts={_SLACK_ANSWER_KEY: {_WORK_URL_KEY: url}})


def _issue_url(raw: RawAPIDict) -> str:
    for key in ("html_url", "web_url", "url"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


__all__ = [
    "FINGERPRINT_MARKER",
    "CodeHostUnresolvableError",
    "EmptyWorkItemError",
    "FiledWorkItem",
    "ForgeRefusedError",
    "NoCodeHostError",
    "NoFilingRepoError",
    "UnresolvableIssueRefError",
    "UntrackableIssueError",
    "WorkItemFilingError",
    "file_work_item",
    "filing_repo",
]
