"""Pre-push gate: cross-check ``Closes/Fixes/Resolves #N`` against the real issue.

teatree's internal TaskList uses integer ids (``#45``, ``#66``, …) that are
NOT GitHub/GitLab issue numbers. A ``Closes #<task-id>`` trailer therefore
mis-targets whatever unrelated issue happens to carry that number — observed:
a closed PR, a docs issue — auto-closing it the instant the PR merges (#83).

This gate runs alongside the auto-close-rewrite path (``sanitize_close_keywords``)
and is its mirror image: ``_close_keyword_gate`` REJECTS trailers for overlays
that forbid them, while this gate VALIDATES the trailers that overlays which
DO auto-close (``config.mr_close_ticket``) are about to ship. For every
``#N`` referenced after a close keyword it resolves the sibling issue URL on
the **target repo** (derived from the ticket's own ``issue_url``) and fetches
the issue via the code host.

BLOCK (``SystemExit``) when the issue is closed or missing — a closed or
absent target is the task-id-vs-issue-number symptom. WARN (logged,
non-blocking) when the issue is open but its title shares no token with the
branch name — a weak "probably the wrong issue" signal.

Detection uses the same close-keyword verb set as
``teatree.core.runners.ship.CLOSE_KEYWORD_RE`` (the auto-close single source
of truth, #1090), narrowed to the *bare* ``#N`` form — full-URL references
already name their target repo, so they carry no task-id collision hazard.
Unverifiable inputs (no code host, an unparsable ticket URL, a git failure)
fail OPEN: the gate skips rather than block on an unknown, matching
``_scan_sources`` and ``PrOpenState.UNKNOWN``.
"""

import logging
import re
from urllib.parse import urlparse

from teatree.core.backend_factory import code_host_from_overlay
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

# A close keyword followed by a *bare* ``#N`` — the form that carries the
# task-id-vs-issue-number hazard. Full-URL references already name their
# target repo explicitly, so they are out of scope here (the close-keyword
# gate covers the forbidding overlays; auto-close handles the rest).
_BARE_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?::\s*|\s+)#(?P<number>\d+)",
    re.IGNORECASE,
)

# A GitHub-/GitLab-style issue URL whose trailing ``/<number>`` we swap for the
# referenced ``#N`` to build the sibling issue URL on the same repo. Matches
# both ``…/issues/<n>`` (GitHub) and ``…/-/issues/<n>`` (GitLab web form).
_ISSUE_PATH_RE = re.compile(r"^(?P<prefix>.*/issues)/\d+/?$")

_MIN_TOKEN_LEN = 3
_OPEN_STATES = frozenset({"open", "opened"})
_CLOSED_STATES = frozenset({"closed", "close"})


def _tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens of *text*, dropping short / numeric ones.

    A leading issue number (``83-gate-…``) and 1-2 char fragments carry no
    intent signal, so they are excluded from the cross-relevance comparison.
    """
    raw = re.split(r"[^0-9a-z]+", text.lower())
    return {tok for tok in raw if len(tok) >= _MIN_TOKEN_LEN and not tok.isdigit()}


def _shares_token(issue_title: str, branch: str) -> bool:
    """Whether *issue_title* and *branch* share any meaningful token.

    Returns ``True`` when the branch has no comparable tokens — a token-less
    branch is undecidable, and the gate must never escalate an undecidable
    case to a warning.
    """
    branch_tokens = _tokens(branch)
    if not branch_tokens:
        return True
    return bool(branch_tokens & _tokens(issue_title))


def _issue_url_for_ref(ticket_issue_url: str, number: str) -> str:
    """Build the sibling issue URL for ``#number`` on the ticket's own repo.

    The ticket's ``issue_url`` names the repo a bare ``#N`` resolves against;
    swap its trailing issue number for *number*. Returns ``""`` when the
    ticket URL is not a recognisable issue URL (the gate then fails open).
    """
    parsed = urlparse(ticket_issue_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    match = _ISSUE_PATH_RE.match(parsed.path)
    if match is None:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{match['prefix']}/{number}"


def _referenced_numbers(worktree: Worktree) -> list[str]:
    """Bare ``#N`` numbers referenced after a close keyword, de-duplicated.

    Scans the same author-intent sources as ``_close_keyword_gate``: the raw
    last-commit message (the MR description's source) plus every branch commit
    body. A git failure is unverifiable — return what was collected so far
    rather than block on an unknown.
    """
    repo = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    branch = worktree.branch
    if not repo or not branch:
        return []
    sources: list[str] = []
    try:
        subject, body = git.last_commit_message(repo=repo)
        sources.append(f"{subject}\n\n{body}" if body else subject)
        base = f"origin/{git.default_branch(repo=repo)}"
        sources.extend(git.commit_messages(repo=repo, range_spec=f"{base}..{branch}"))
    except (CommandFailedError, RuntimeError, ValueError):
        pass
    numbers: list[str] = []
    for source in sources:
        for match in _BARE_REF_RE.finditer(source):
            number = match["number"]
            if number not in numbers:
                numbers.append(number)
    return numbers


def _issue_state(issue: RawAPIDict) -> str:
    state = issue.get("state")
    return state.lower() if isinstance(state, str) else ""


def _issue_title(issue: RawAPIDict) -> str:
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def _issue_missing(issue: RawAPIDict) -> bool:
    return "error" in issue or _issue_state(issue) not in (_OPEN_STATES | _CLOSED_STATES)


def run_closes_issue_crosscheck(ticket: Ticket, worktree: Worktree) -> None:
    """Cross-check every ``Closes #N`` trailer against the real target issue.

    No-op unless the active overlay sets ``config.mr_close_ticket`` (only
    then does ``Closes #N`` survive into the merged PR and auto-close the
    issue). Raises ``SystemExit`` when a referenced issue is closed or
    missing; logs a WARNING when an open issue's title shares no token with
    the branch. Fails OPEN on any unverifiable input.
    """
    if not get_overlay().config.mr_close_ticket:
        return

    numbers = _referenced_numbers(worktree)
    if not numbers:
        return

    host = code_host_from_overlay()
    if host is None:
        return

    branch = worktree.branch
    blockers: list[str] = []
    for number in numbers:
        url = _issue_url_for_ref(ticket.issue_url, number)
        if not url:
            # Ticket URL is not a recognisable issue URL — can't resolve the
            # target repo for a bare ``#N``; unverifiable, skip.
            continue
        issue = host.get_issue(url)
        if _issue_missing(issue):
            blockers.append(
                f"  - #{number} → {url}\n    issue is missing or unreadable on the target repo "
                "(a teatree task id is NOT an issue number — did you mean a real open issue?)"
            )
            continue
        if _issue_state(issue) in _CLOSED_STATES:
            blockers.append(
                f"  - #{number} → {url}\n    issue is already CLOSED — merging would re-close it spuriously"
            )
            continue
        if not _shares_token(_issue_title(issue), branch):
            logger.warning(
                "Closes #%s targets %r whose title %r shares no token with branch %r — "
                "verify this is the intended issue, not a stray task-id collision.",
                number,
                url,
                _issue_title(issue),
                branch,
            )

    if blockers:
        bullets = "\n".join(blockers)
        msg = (
            "Refusing to ship: a `Closes/Fixes/Resolves #N` trailer references an issue that is "
            "not a real open issue on the target repo. teatree task ids are NOT issue numbers, so "
            "a stray trailer mis-targets an unrelated issue and auto-closes it on merge:\n"
            f"{bullets}\n"
            "Fix the trailer to reference a real OPEN issue, or rewrite it to `Relates to #N` "
            "(no auto-close), then retry `pr create`."
        )
        raise SystemExit(msg)
