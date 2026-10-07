"""Who "we" are on a forge, and the guard that keeps the factory off other people's tickets (#162).

Rule 5 of #162: the factory may extend, fold into, or close a ticket
ONLY when the owner, the factory bot, or the CI workflows of a repo in the
owner's own namespace (``github-actions[bot]``) filed it. Everything else in
:mod:`teatree.core.issue_hygiene` is a write, so this module is the authority
every one of those writes asks first.

The self-set already existed three times over — ``user_identity_aliases``
(:func:`teatree.core.backend_factory._resolved_identities`), the per-host
``self_forge_identities`` map (:mod:`teatree.hooks.foreign_mr_cli`,
:mod:`teatree.core.review.review_candidate`), and each host's
``current_user()`` (:func:`teatree.loop.scanner_factory_broadcast_claims._self_forge_identities`).
Three derivations of one question drift; this is the fourth-and-only one the
hygiene facade consumes, and it adds **no new setting** — it unions the rows
that already exist.

What it deliberately does NOT union in is ``trusted_issue_authors`` /
:mod:`teatree.core.review.author_trust`. Those widen on purpose to colleagues so
a teammate can approve a merge. Widening *self* with them would let the factory
rewrite a colleague's ticket description, which is the precise harm Rule 5
exists to prevent.

Every unresolved answer fails CLOSED. An author-less payload, an unreadable
issue, an ``{"error": ...}`` response and a forge exception are all "not provably
ours", which means refused — never "probably ours, go ahead".
"""

import logging
from collections.abc import Iterable
from urllib.parse import urlparse

from teatree.core.backend_protocols import CodeHostBackend
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

SELF_FORGE_IDENTITIES_SETTING = "self_forge_identities"

# (container key, login key) per forge payload shape: GitLab issues nest the
# author under ``author.username``, GitHub under ``user.login``.
_AUTHOR_PATHS = (("author", "username"), ("user", "login"))

# The login GitHub Actions files issues under: on a repo we own, those workflows are ours.
_WORKFLOW_BOT_LOGIN = "github-actions[bot]"
NOT_SELF_AUTHORED_REASON = "not authored by the owner, the factory bot, or our own repo's workflows"


class ExternalIssueRefusedError(Exception):
    """Raised when a mutating issue operation targets a ticket we did not file.

    Carries the resolved *author* (empty when the forge would not say) so a
    caller can report *why* it refused without re-reading the issue.
    """

    def __init__(self, issue_url: str, author: str, reason: str) -> None:
        self.issue_url = issue_url
        self.author = author
        self.reason = reason
        shown = author or "(unknown)"
        super().__init__(f"refusing to modify {issue_url}: authored by {shown} — {reason}")


def issue_author_login(issue: object) -> str:
    """The author login on an issue payload, across both forge shapes, or ``""``.

    ``""`` means "the payload does not prove an author" — a missing key, a
    non-dict author node, or a blank login. Callers treat that as external.
    """
    if not isinstance(issue, dict):
        return ""
    for container, key in _AUTHOR_PATHS:
        node = issue.get(container)
        if not isinstance(node, dict):
            continue
        login = node.get(key)
        if isinstance(login, str) and login.strip():
            return login.strip()
    return ""


def _configured_aliases() -> tuple[str, ...]:
    """The owner's declared cross-forge handles (``user_identity_aliases``)."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: call-time import, kept lazy

    return tuple(get_effective_settings().user_identity_aliases)


def declared_identities_for_url(url: str) -> tuple[str, ...]:
    """The logins declared as our own bots for *url*'s forge host, as written.

    The URL-keyed face of the ONE ``self_forge_identities`` read
    (:func:`teatree.hooks.foreign_mr_cli.declared_self_identities_raw`), so a bot
    declared once is honoured by the push gate, the review-candidate self-author
    skip and this guard alike — three hand-rolled reads of one setting is how they
    drift. An unparsable URL declares nothing, which refuses rather than widens.
    """
    from teatree.hooks.foreign_mr_cli import (  # noqa: PLC0415 — deferred: keeps the cold hook leaf cold
        declared_self_identities_raw,
    )

    try:
        host = (urlparse(url).hostname or "").casefold()
    except ValueError:
        return ()
    return declared_self_identities_raw(host)


def self_identity_set(issue_url: str, *, host: CodeHostBackend | None = None) -> set[str]:
    """Every case-folded login that counts as us for *issue_url*.

    The union of the owner's aliases, the bots declared for that forge host, and
    — when *host* is given — the login its own token authenticates as. A host
    that cannot name itself narrows the set rather than raising: a narrower self
    set refuses more, which is the safe direction.
    """
    names = [*_configured_aliases(), *declared_identities_for_url(issue_url)]
    if host is not None:
        try:
            names.append(host.current_user())
        except Exception:  # a host that cannot name itself narrows the self-set, which refuses more, not less
            logger.warning("self-identity: current_user() failed for %s", issue_url, exc_info=True)
    return {name.strip().casefold() for name in names if isinstance(name, str) and name.strip()}


def issue_author_is_self(author: str, identities: Iterable[str]) -> bool:
    """Whether *author* is one of *identities*, comparing case-folded.

    A blank author is never self — an unprovable author is external.
    """
    if not author or not author.strip():
        return False
    return author.strip().casefold() in {name.strip().casefold() for name in identities if name and name.strip()}


def require_self_authored_issue(*, host: CodeHostBackend, issue_url: str) -> RawAPIDict:
    """Re-read *issue_url* and return it, or raise :class:`ExternalIssueRefusedError`.

    The re-read is the point. A candidate fetched during discovery is not
    authority to write: an issue can be transferred, imported, or re-authored
    between the scan and the mutation, so every mutating facade method asks
    again immediately before its write and uses THIS payload (returned here, so
    the caller writes against the same body it was authorised on).
    """
    try:
        fresh = host.get_issue(issue_url)
    except Exception as exc:  # an unreadable issue is refused, never assumed ours
        raise ExternalIssueRefusedError(issue_url, "", f"could not read the issue ({exc})") from exc
    if not isinstance(fresh, dict) or fresh.get("error"):
        detail = fresh.get("error") if isinstance(fresh, dict) else "non-dict response"
        raise ExternalIssueRefusedError(issue_url, "", f"the forge did not return the issue ({detail})")
    author = issue_author_login(fresh)
    identities = self_identity_set(issue_url, host=host)
    if not (issue_author_is_self(author, identities) or _is_own_repo_workflow_bot(author, issue_url, identities)):
        raise ExternalIssueRefusedError(issue_url, author, NOT_SELF_AUTHORED_REASON)
    return fresh


def _is_own_repo_workflow_bot(author: str, issue_url: str, identities: set[str]) -> bool:
    """Whether *author* is the workflow bot of a repo whose namespace is one of *identities*."""
    if author.casefold() != _WORKFLOW_BOT_LOGIN:
        return False
    try:
        namespace = urlparse(issue_url).path.strip("/").split("/", 1)[0]
    except ValueError:
        return False
    return issue_author_is_self(namespace, identities)


__all__ = [
    "NOT_SELF_AUTHORED_REASON",
    "SELF_FORGE_IDENTITIES_SETTING",
    "ExternalIssueRefusedError",
    "declared_identities_for_url",
    "issue_author_is_self",
    "issue_author_login",
    "require_self_authored_issue",
    "self_identity_set",
]
