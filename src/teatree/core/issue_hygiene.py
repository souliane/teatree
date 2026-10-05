"""The single application-level issue-write facade: the description is the source of truth (#162).

Many downstream tickets carried their real requirements in COMMENTS. A lane
reads the description, so those requirements were simply never executed. The fix
is not "also read the comments" — it is to stop normative text ever landing in a
comment, which means one seam owns every issue write and decides where the text
goes.

Five rules, each enforced here rather than in an agent-authored skill:

1.  **Dedupe before create.** :func:`create_or_extend` refuses to file until every
    OPEN ticket in the repo has been judged. A fitting ticket is extended; a new
    one records every rejected candidate and why, in its own body.
2.  **No requirement comments.** :func:`record_issue_note` takes a closed
    :class:`NotePurpose`. The four normative purposes append a dated description
    section; only ``status`` and ``evidence`` remain comments. There is no untyped
    note API.
3.  **A sweep posts no comments.** Passing ``sweep_run_id`` refuses *every* comment
    purpose, so the zero-comment invariant holds below the skill that is supposed
    to honour it.
4.  **Every sweep mutation is attributable.** A ``sweep_run_id`` is recorded against
    the :class:`~teatree.core.models.TicketSweepRun` row, which is what makes the
    "changes tend to zero" metric a measurement rather than a claim.
5.  **Our tickets only.** Every mutating path — description appends, closes and
    ``status`` / ``evidence`` comments alike — calls
    :func:`~teatree.core.self_forge_identities.require_self_authored_issue`, and a
    description write goes against the payload IT returned, so authority and body
    come from the same fetch.

**On conditional updates.** Neither forge offers a precondition on an issue
update — GitHub's ``PATCH /issues/:n`` takes no ``If-Match`` and GitLab's ``PUT``
takes no version — so the append emulates a compare-and-swap: rebase the section
onto the body just read, re-read immediately before the write and rebase again if
the body moved, write, then re-read and verify the marker landed AND the base text
survived. A lost race retries onto the newest body, bounded by
:data:`MAX_APPEND_ATTEMPTS`, after which it raises rather than reporting success.

What that does NOT close, and cannot without a forge precondition: a writer whose
compare read comes before our write and whose own write comes after it. Two such
writers each see a clean compare, and the later ``PUT`` replaces the earlier
section. If that later write lands after our verification re-read, we have
already reported success and the loss is invisible here. The emulation shrinks
that window from a writer's whole read-to-write span to one read-to-``PUT``
round-trip; it does not make it zero.
"""

import hashlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.issue_writes.section_removal import DescriptionRemoval
from teatree.core.issue_writes.section_removal import remove_description_section as _remove_description_section
from teatree.core.self_forge_identities import ExternalIssueRefusedError, require_self_authored_issue
from teatree.core.send_proxy import forge_from_url, route_forge_write
from teatree.types import RawAPIDict
from teatree.utils.url_slug import project_slug_from_ref

logger = logging.getLogger(__name__)

MAX_APPEND_ATTEMPTS = 4
_MARKER_PREFIX = "<!-- t3-hygiene:"
_DEDUPE_HEADING = "Dedupe check"


class NotePurpose(StrEnum):
    """Why a note is being recorded — the closed set that decides body vs comment.

    The first four are NORMATIVE: they change what the ticket asks for, so they
    belong in the description where a lane reads them. ``status`` and ``evidence``
    report on work rather than specifying it, so they stay comments (outside a
    sweep).
    """

    REQUIREMENT = "requirement"
    CHANGE_REQUEST = "change_request"
    SCOPE_CHANGE = "scope_change"
    DECISION = "decision"
    STATUS = "status"
    EVIDENCE = "evidence"


NORMATIVE_PURPOSES = frozenset(
    {NotePurpose.REQUIREMENT, NotePurpose.CHANGE_REQUEST, NotePurpose.SCOPE_CHANGE, NotePurpose.DECISION}
)

_PURPOSE_HEADINGS = {
    NotePurpose.REQUIREMENT: "Requirement added",
    NotePurpose.CHANGE_REQUEST: "Change request",
    NotePurpose.SCOPE_CHANGE: "Scope change",
    NotePurpose.DECISION: "Decision",
}


class SweepCommentRefusedError(Exception):
    """Raised when a sweep tries to post a comment. Rule 3 admits no exception."""


class IssueWriteConflictError(Exception):
    """Raised when a description append could not be confirmed on the live issue.

    Retryable by the caller. Never swallowed into a success: an append that the
    forge did not keep means the requirement is not where the lane will look.
    """


@dataclass(frozen=True)
class AppendOutcome:
    """The result of one description append."""

    kind: Literal["appended", "already_present"]
    issue_url: str
    digest: str
    attempts: int = 1


@dataclass(frozen=True)
class NoteOutcome:
    """Where a note ended up."""

    kind: Literal["appended", "commented"]
    issue_url: str
    purpose: NotePurpose
    digest: str = ""
    comment_id: int = 0


@dataclass(frozen=True)
class IssueDraft:
    """The issue a caller wants to exist — filed as-is, or folded into a fitting ticket.

    ``action`` is the filer's own audit label, carried on the draft rather than passed
    beside it so one scrub serves both the leak gate and an audit trail that says WHICH
    filer wrote: a caller pre-scrubbing to keep its label would be scrubbed and audited
    twice for one create.
    """

    repo: str
    title: str
    body: str
    labels: tuple[str, ...] = ()
    action: str = "issue_create"
    forge: str = ""


@dataclass(frozen=True)
class DescriptionSection:
    """One dated section to append, and the audit label of whoever is appending it.

    ``action`` is the ``IssueDraft.action`` twin: one scrub serves both the leak gate
    and a ``SendAudit`` row that says WHICH writer appended, instead of every append
    in the system looking alike.
    """

    heading: str
    body: str
    action: str = "issue_description_append"
    identity: str = ""


@dataclass(frozen=True)
class CreateDecision:
    """One judgment about one open candidate.

    ``fits`` marks the candidate the request should be folded into; otherwise
    *reason* must say why it was rejected, and that reason is written into the
    new ticket's body so the next reader can see the search actually happened.
    """

    candidate_url: str
    fits: bool = False
    reason: str = ""


@dataclass(frozen=True)
class CreateOutcome:
    """What a create attempt actually did."""

    kind: Literal["created_new", "extended_existing", "external_conflict", "stale_snapshot"]
    issue_url: str = ""
    unjudged: tuple[str, ...] = field(default_factory=tuple)


def section_marker(digest: str) -> str:
    """The idempotency marker embedded in an appended section."""
    return f"{_MARKER_PREFIX}{digest} -->"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _description_of(issue: RawAPIDict) -> str:
    """The body, across GitLab (``description``) and GitHub (``body``)."""
    for key in ("description", "body"):
        value = issue.get(key)
        if isinstance(value, str):
            return value
    return ""


def _issue_url_of(issue: RawAPIDict) -> str:
    for key in ("web_url", "html_url", "url"):
        value = issue.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _scrub(text: str, *, target: str, repo: str, action: str, forge: str = "") -> str:
    """Route outbound text through the shared leak-gate/send-proxy seam.

    Every newly authored section or comment this module writes passes the SAME
    scrub the MCP and CLI writers use, so moving a requirement
    from a comment into a description cannot smuggle a customer codename onto a
    public forge.
    """
    return route_forge_write(forge=forge or forge_from_url(target), repo=repo, text=text, action=action, target=target)


def append_description_section(
    *,
    host: CodeHostBackend,
    issue_url: str,
    section: DescriptionSection,
    sweep_run_id: str = "",
) -> AppendOutcome:
    """Append one dated, marked section to *issue_url*'s description, or confirm it is already there.

    The ONE compare-and-append primitive behind create-dedupe, requirement
    routing and sweep folding. Authority comes first — the Rule 5 guard runs
    before the outbound is even scrubbed, so a colleague's ticket is refused
    without auditing a write that was never going to happen — and the payload it
    returns is the base the section is rebased onto, so authority and body are
    one fetch. Just before each write the description is re-read and compared
    with that base, and after the write it is re-read again: the marker must be
    there and the base text must have survived. A lost race re-proves authority
    and rebases onto the newest body; an unconfirmed write after
    :data:`MAX_APPEND_ATTEMPTS` raises :class:`IssueWriteConflictError` rather
    than reporting a success the forge did not keep.

    """
    digest = section.identity or _digest(section.heading, section.body.strip())
    marker = section_marker(digest)
    fresh = require_self_authored_issue(host=host, issue_url=issue_url)
    repo = _resolve_repo(host, issue_url)
    text = _scrub(section.body, target=issue_url, repo=repo, action=section.action)
    rendered = f"{marker}\n\n## {section.heading} ({_today()})\n\n{text.strip()}"

    for attempt in range(1, MAX_APPEND_ATTEMPTS + 1):
        current = _description_of(fresh)
        if marker in current:
            return AppendOutcome(kind="already_present", issue_url=issue_url, digest=digest, attempts=attempt)
        if _description_of(host.get_issue(issue_url)) == current:
            proposed = f"{current}\n\n{rendered}" if current else rendered
            _raise_on_transport_error(host.update_issue(issue_url=issue_url, body=proposed))
            landed = _description_of(host.get_issue(issue_url))
            if marker in landed and current in landed:
                _record_sweep_change(sweep_run_id, issue_url)
                return AppendOutcome(kind="appended", issue_url=issue_url, digest=digest, attempts=attempt)
        logger.info("issue-hygiene: append to %s lost a race on attempt %d — rebasing", issue_url, attempt)
        if attempt < MAX_APPEND_ATTEMPTS:
            fresh = require_self_authored_issue(host=host, issue_url=issue_url)

    msg = f"could not confirm the description append on {issue_url} after {MAX_APPEND_ATTEMPTS} attempts"
    raise IssueWriteConflictError(msg)


def remove_description_section(*, host: CodeHostBackend, issue_url: str, removal: DescriptionRemoval) -> None:
    """Remove one marked section through the shared issue-write facade."""
    _remove_description_section(
        host=host,
        issue_url=issue_url,
        removal=removal,
        scrub=lambda body: _scrub(body, target=issue_url, repo=_resolve_repo(host, issue_url), action=removal.action),
        write=lambda body: _raise_on_transport_error(host.update_issue(issue_url=issue_url, body=body)),
    )


@dataclass(frozen=True)
class CloseOutcome:
    """What a close attempt did."""

    kind: Literal["closed", "external_refused"]
    issue_url: str
    digest: str = ""


def close_with_rationale(
    *,
    host: CodeHostBackend,
    issue_url: str,
    rationale: str,
    sweep_run_id: str = "",
    action: str = "issue_close_rationale",
) -> CloseOutcome:
    """Record *why* in the DESCRIPTION, then close with no comment (#162 Rules 2 and 3).

    ``close_issue(comment=...)`` is a hidden comment seam: the reason a ticket was
    retired is exactly the kind of text a later reader needs, and a comment is where
    nothing looks. So the two halves are split — the rationale becomes a dated
    description section, and only then does the issue close, carrying no comment.

    Order is load-bearing. A close whose reason failed to land is a ticket retired
    for no visible cause, so an unconfirmed append (or a leak refusal, which raises
    out of the scrub) leaves the issue OPEN and retryable. The reverse order would
    close first and lose the reason.

    Rule 5 applies: the guard runs inside the append, so a ticket someone else filed
    is neither annotated nor closed — it comes back as ``external_refused``.
    """
    try:
        outcome = append_description_section(
            host=host,
            issue_url=issue_url,
            section=DescriptionSection(heading="Closed", body=rationale, action=action),
            sweep_run_id=sweep_run_id,
        )
    except ExternalIssueRefusedError:
        return CloseOutcome(kind="external_refused", issue_url=issue_url)
    _raise_on_transport_error(host.close_issue(issue_url=issue_url))
    return CloseOutcome(kind="closed", issue_url=issue_url, digest=outcome.digest)


def record_issue_note(
    *,
    host: CodeHostBackend,
    issue_url: str,
    purpose: NotePurpose | str,
    content: str,
    sweep_run_id: str = "",
) -> NoteOutcome:
    """Record *content* against *issue_url* in the place its *purpose* belongs.

    Normative purposes append a description section; ``status`` / ``evidence``
    post a comment, unless a sweep is active — Rule 3 refuses every comment while
    ``sweep_run_id`` is set. There is deliberately no default purpose: a caller
    that has not decided whether it is writing the specification or reporting on
    it has not decided where the text belongs.
    """
    try:
        resolved = NotePurpose(purpose)
    except ValueError as exc:
        allowed = ", ".join(p.value for p in NotePurpose)
        msg = f"an issue note needs an explicit purpose — one of: {allowed}"
        raise ValueError(msg) from exc
    if not content or not content.strip():
        msg = "an issue note needs non-empty content"
        raise ValueError(msg)

    if resolved in NORMATIVE_PURPOSES:
        outcome = append_description_section(
            host=host,
            issue_url=issue_url,
            section=DescriptionSection(
                heading=_PURPOSE_HEADINGS[resolved], body=content, action=f"issue_note_{resolved.value}"
            ),
            sweep_run_id=sweep_run_id,
        )
        return NoteOutcome(kind="appended", issue_url=issue_url, purpose=resolved, digest=outcome.digest)

    if sweep_run_id:
        msg = f"a sweep posts no comments — {resolved.value} on {issue_url} must fold into the description instead"
        raise SweepCommentRefusedError(msg)

    require_self_authored_issue(host=host, issue_url=issue_url)
    repo = _resolve_repo(host, issue_url)
    text = _scrub(content, target=issue_url, repo=repo, action=f"issue_note_{resolved.value}")
    raw = _raise_on_transport_error(host.post_issue_comment(issue_url=issue_url, body=text))
    comment_id = raw.get("id")
    return NoteOutcome(
        kind="commented",
        issue_url=issue_url,
        purpose=resolved,
        comment_id=comment_id if isinstance(comment_id, int) else 0,
    )


def create_or_extend(
    *,
    host: CodeHostBackend,
    draft: IssueDraft,
    decisions: Sequence[CreateDecision],
    snapshot_urls: Iterable[str] | None = None,
    sweep_run_id: str = "",
) -> CreateOutcome:
    """File *draft* — but only after the open backlog has been judged.

    *decisions* must cover every currently-open ticket: one marked ``fits`` folds
    the request into that ticket's description instead of filing; otherwise every
    candidate needs a rejection *reason*, and those reasons are written into the
    new ticket so the dedupe is auditable rather than asserted.

    *snapshot_urls*, when given, is the backlog the decisions were made against.
    A ticket filed since then makes the judgment set incomplete, so the outcome is
    ``stale_snapshot`` and the caller judges again — the window in which two
    lanes file the same ticket is exactly this one.
    """
    live = open_candidates(host=host, repo=draft.repo)
    if snapshot_urls is not None:
        appeared = tuple(sorted(set(live) - set(snapshot_urls)))
        if appeared:
            return CreateOutcome(kind="stale_snapshot", unjudged=appeared)
    return _commit(host=host, draft=draft, live=live, decisions=decisions, sweep_run_id=sweep_run_id)


def create_or_extend_by_marker(
    *,
    host: CodeHostBackend,
    draft: IssueDraft,
    marker: str,
    sweep_run_id: str = "",
) -> CreateOutcome:
    """File *draft*, judging the backlog mechanically: a candidate fits iff its body carries *marker*.

    The entry point for a DETERMINISTIC filer — a retro escalation, a dream
    reconciliation — which already has a stable fingerprint and needs no agent to
    read the tracker. It is not a bypass: the backlog pass still happens, from the
    same single listing the commit is judged against, so a ticket that appeared
    mid-call cannot leave the judgment set incomplete. That shared listing is the
    reason this is a sibling of :func:`create_or_extend` rather than a wrapper
    around it — two independent reads would race into a spurious refusal.
    """
    if not marker.strip():
        msg = "an empty marker would judge every open ticket as fitting — pass the draft's fingerprint marker"
        raise ValueError(msg)
    live = open_candidates(host=host, repo=draft.repo)
    decisions = [
        CreateDecision(
            candidate_url=url,
            fits=marker in _description_of(issue),
            reason="" if marker in _description_of(issue) else f"does not carry the marker {marker}",
        )
        for url, issue in live.items()
    ]
    return _commit(host=host, draft=draft, live=live, decisions=decisions, sweep_run_id=sweep_run_id)


def open_candidates(*, host: CodeHostBackend, repo: str) -> dict[str, RawAPIDict]:
    """Every OPEN ticket on *repo*, keyed by URL — the fresh landscape no create may skip.

    Deliberately not backed by ``LandscapeArtifact``: that is ticket-scoped and may
    be stale, and a stale backlog is how two lanes file the same ticket.
    """
    return {url: issue for issue in host.list_repo_open_issues(repo=repo) if (url := _issue_url_of(issue))}


def _commit(
    *,
    host: CodeHostBackend,
    draft: IssueDraft,
    live: dict[str, RawAPIDict],
    decisions: Sequence[CreateDecision],
    sweep_run_id: str = "",
) -> CreateOutcome:
    """Apply *decisions* to the *live* backlog: extend the fitting ticket, or file once."""
    judged = {decision.candidate_url for decision in decisions}
    unjudged = tuple(sorted(set(live) - judged))
    if unjudged:
        msg = f"refusing to file on {draft.repo}: {len(unjudged)} open ticket(s) unjudged — {', '.join(unjudged)}"
        raise ValueError(msg)

    fitting = [decision for decision in decisions if decision.fits]
    if fitting:
        return _extend(host=host, target=fitting[0].candidate_url, draft=draft, sweep_run_id=sweep_run_id)

    unexplained = [d.candidate_url for d in decisions if d.candidate_url in live and not d.reason.strip()]
    if unexplained:
        msg = f"refusing to file on {draft.repo}: no rejection reason for {', '.join(sorted(unexplained))}"
        raise ValueError(msg)

    return _create(host=host, draft=draft, decisions=decisions, sweep_run_id=sweep_run_id)


def _extend(*, host: CodeHostBackend, target: str, draft: IssueDraft, sweep_run_id: str) -> CreateOutcome:
    """Fold the request into the fitting ticket, or report an external conflict.

    A fitting ticket somebody else filed is a genuine collision a human must
    resolve: it is neither edited nor duplicated, because filing our own copy
    alongside it is the duplication this rule exists to stop.
    """
    try:
        append_description_section(
            host=host,
            issue_url=target,
            section=DescriptionSection(heading="Request added", body=draft.body, action=draft.action),
            sweep_run_id=sweep_run_id,
        )
    except ExternalIssueRefusedError:
        return CreateOutcome(kind="external_conflict", issue_url=target)
    return CreateOutcome(kind="extended_existing", issue_url=target)


def _create(
    *, host: CodeHostBackend, draft: IssueDraft, decisions: Sequence[CreateDecision], sweep_run_id: str
) -> CreateOutcome:
    """File the new ticket with the dedupe audit appended to its body.

    A label rides the leak scrub too — a forge auto-creates a missing one, so a
    customer codename in a label reaches a public forge exactly like a body would.
    """
    repo, action = draft.repo, draft.action
    clean_title = _scrub(draft.title, target=repo, repo=repo, action=action, forge=draft.forge)
    clean_body = _scrub(draft.body, target=repo, repo=repo, action=action, forge=draft.forge)
    clean_labels = [_scrub(label, target=repo, repo=repo, action=action, forge=draft.forge) for label in draft.labels]
    clean_dedupe_audit = _scrub(_dedupe_audit(decisions), target=repo, repo=repo, action=action, forge=draft.forge)
    raw = _raise_on_transport_error(
        host.create_issue(
            repo=repo,
            title=clean_title,
            body=f"{clean_body}\n\n{clean_dedupe_audit}",
            labels=clean_labels or None,
        )
    )
    issue_url = _issue_url_of(raw)
    _record_sweep_change(sweep_run_id, issue_url)
    return CreateOutcome(kind="created_new", issue_url=issue_url)


def _dedupe_audit(decisions: Sequence[CreateDecision]) -> str:
    """The searched-and-rejected record that goes into a newly filed ticket's body.

    An empty backlog says so explicitly. "No candidates" and "nobody looked" read
    identically otherwise, and the second is the one worth catching.
    """
    if not decisions:
        return f"## {_DEDUPE_HEADING} ({_today()})\n\nNo open candidates existed on this repo."
    lines = [f"- {d.candidate_url} — {d.reason.strip()}" for d in decisions if not d.fits]
    return f"## {_DEDUPE_HEADING} ({_today()})\n\nRejected candidates:\n" + "\n".join(lines)


def _raise_on_transport_error(raw: RawAPIDict) -> RawAPIDict:
    """Surface a forge ``{"error": ...}`` as a retryable failure rather than a silent success.

    The transports answer an unresolvable project or URL with an error dict, not
    an exception. Letting that through would report a requirement as recorded
    when nothing was written — the exact invisibility this module exists to end.
    """
    error = raw.get("error") if isinstance(raw, dict) else None
    if error:
        raise IssueWriteConflictError(str(error))
    return raw if isinstance(raw, dict) else {}


def _resolve_repo(host: CodeHostBackend, issue_url: str) -> str:
    """The repo slug for *issue_url*, falling back to its parsed URL path.

    The slug feeds the leak gate and send-proxy allowlist. An unrecognised URL
    remains the destination, so the gate still scans it conservatively.
    """
    try:
        resolved = host.repo_for_issue_url(issue_url)
    except Exception:  # an unresolvable slug degrades the gate's lookup, never the scan
        logger.warning("issue-hygiene: could not resolve a repo slug for %s", issue_url, exc_info=True)
        resolved = ""
    return project_slug_from_ref(resolved) or project_slug_from_ref(issue_url) or resolved or issue_url


def _record_sweep_change(sweep_run_id: str, issue_url: str) -> None:
    """Attribute a landed mutation to its sweep run, if one is active.

    Best-effort by design: the forge write has already happened, so failing to
    record the count must not turn a successful fold into an exception. The run
    row stays visibly incomplete instead, which the doctor check surfaces.
    """
    if not sweep_run_id:
        return
    from teatree.core.models import TicketSweepRun  # noqa: PLC0415 — deferred: ORM import kept out of module load

    try:
        TicketSweepRun.objects.record_change(run_id=sweep_run_id, issue_url=issue_url)
    except Exception:  # the write landed, so a bookkeeping failure must not undo it
        logger.warning(
            "issue-hygiene: could not record %s against sweep run %s", issue_url, sweep_run_id, exc_info=True
        )


__all__ = [
    "MAX_APPEND_ATTEMPTS",
    "NORMATIVE_PURPOSES",
    "AppendOutcome",
    "CloseOutcome",
    "CreateDecision",
    "CreateOutcome",
    "DescriptionRemoval",
    "DescriptionSection",
    "IssueDraft",
    "IssueWriteConflictError",
    "NoteOutcome",
    "NotePurpose",
    "SweepCommentRefusedError",
    "append_description_section",
    "close_with_rationale",
    "create_or_extend",
    "create_or_extend_by_marker",
    "open_candidates",
    "record_issue_note",
    "remove_description_section",
    "section_marker",
]
