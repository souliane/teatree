r"""Deterministic, code-only reference linkifier for outbound messages.

The "Clickable References" rule — a bare issue/MR ref (``#1500``, ``!6301``,
``owner/repo#42``) must ship to a user-facing surface as a clickable link.
A historical PreToolUse/Stop bare-reference *blocking* gate enforced this by
DETECTING a bare ref and BLOCKING the message, asking the **model** to rewrite
it. That rewrite was non-deterministic (the model could fabricate the wrong
URL or miss a ref), cost a model round-trip, was fragile, and over-blocked
unrelated commands — so the blocking gate was removed.

This module is the deterministic, code-only mechanism that replaces it: it
resolves each bare ref to its canonical URL from teatree's own Python DB (and
a repo-context construction fallback) and rewrites the message in code — no
model call, no blocking.

Two layers:

:class:`ReferenceResolver` — maps an integer ``N`` (plus optional
``owner/repo`` slug) to a canonical URL or ``None``:

1.  **DB hit first.** ``PullRequest(repo, iid) -> url`` is the canonical
    number-keyed store and also disambiguates issue-vs-PR exactly; ``Ticket``
    issue numbers resolve via ``issue_url``. A stored URL is used verbatim.
2.  **Construction fallback.** Otherwise build the URL deterministically from
    the active repo context (host + ``owner/repo`` slug, from the overlay
    ``code_host`` + the repo's git remote): GitHub ``#N`` ->
    ``.../issues/N`` (GitHub redirects issues<->pull, safe for both), GitLab
    ``!N`` -> ``.../-/merge_requests/N`` and ``#N`` -> ``.../-/issues/N``.
3.  If neither resolves, return ``None`` — the ref is left untouched and the
    gate handles it.

:func:`linkify` — a pure function that detects bare refs in text and rewrites
each to GitHub-flavoured markdown ``[ref](url)``. It SKIPS refs already inside
a markdown link, an inline code span, or a fenced code block (the same
stash/protect approach :mod:`teatree.slack_mrkdwn` uses), is idempotent
(running twice never double-wraps), and leaves an unresolvable ref untouched.

The Slack-mrkdwn surface keeps using :func:`teatree.slack_mrkdwn.slack_linkify`
(``<url|label>`` form); both share the same resolver so a single resolution
order governs every surface. The two overlay hooks
``OverlayBase.resolve_mr_token`` / ``resolve_issue_token`` delegate to this
resolver by default, so every existing ``slack_linkify`` call site resolves
for real instead of leaving every ref bare.
"""

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)

#: ``(N) -> url | None`` — the resolver shape :func:`linkify` and
#: :func:`teatree.slack_mrkdwn.slack_linkify` both consume.
TokenResolver = Callable[[int], str | None]

# Spans excised before bare-token matching: a ref already inside any of these
# is clickable / verbatim and must not be re-wrapped (idempotency + skip).
_MD_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[[^\]]*\]\([^)]*\)")
_FENCED_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE: Final[re.Pattern[str]] = re.compile(r"`[^`\n]+`")

# Bare reference tokens. ``owner/repo#N`` is matched FIRST (longest form) so a
# cross-repo ref is never split into a bare ``#N`` whose repo context is wrong.
_SLUG_ISSUE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_/])([A-Za-z0-9][\w.-]*/[\w.-]+)#(\d+)(?![A-Za-z0-9_])",
)
_BARE_MR_RE: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z0-9_/])!(\d+)(?![A-Za-z0-9_])")
_BARE_ISSUE_RE: Final[re.Pattern[str]] = re.compile(r"(?<![A-Za-z0-9_/])#(\d+)(?![A-Za-z0-9_])")

# Code-host slugs selecting the URL-construction branch in ``_construct`` (which
# documents why GitLab nests under ``/-/`` and GitHub shares one ``/issues/`` path).
_GITHUB_HOST: Final[str] = "github"
_GITLAB_HOST: Final[str] = "gitlab"


class ReferenceResolver:
    """Resolve a bare ref to a canonical URL — DB first, construction fallback.

    Overlay-agnostic. The ``code_host`` ("github"/"gitlab") and the active
    repo's ``owner/repo`` slug are injected so the resolver never imports an
    overlay or shells out itself; :meth:`from_overlay` builds the production
    instance from the active overlay + its first repo's git remote.
    """

    def __init__(self, *, code_host: str = "", default_slug: str = "", web_base: str = "") -> None:
        self._code_host = (code_host or "").strip().lower()
        self._default_slug = default_slug.strip().strip("/")
        # ``web_base`` is the host's web origin (e.g. ``https://gitlab.com``),
        # NOT the API base — construction builds human-facing URLs.
        self._web_base = web_base.rstrip("/")

    @classmethod
    def from_overlay(cls, overlay: "OverlayBase | None" = None) -> "ReferenceResolver":
        """Build a resolver from the active overlay's host + first repo's remote.

        Best-effort: any failure to resolve the overlay, its repos, or a git
        remote yields a resolver that still serves DB hits and degrades the
        construction fallback to ``None`` (the ref is left bare — never a
        guessed-wrong URL). Construction is never crashed onto a CLI turn.
        """
        if overlay is None:
            try:
                from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: call-time import

                overlay = get_overlay()
            except Exception:  # noqa: BLE001 — overlay resolution is best-effort
                return cls()
        code_host = (getattr(overlay.config, "code_host", "") or "").strip().lower()
        slug, web_base = cls._repo_context(overlay)
        return cls(code_host=code_host, default_slug=slug, web_base=web_base)

    @staticmethod
    def _repo_context(overlay: "OverlayBase") -> tuple[str, str]:
        """Return ``(owner/repo slug, web_base)`` from the overlay's first repo remote."""
        from teatree.utils import git, git_remote  # noqa: PLC0415 — deferred: call-time import, kept lazy

        try:
            repos = overlay.get_repos()
        except Exception:  # noqa: BLE001 — best-effort; no repo context means no construction
            return "", ""
        for repo in repos:
            try:
                remote = git.remote_url(repo=repo)
            except Exception as exc:  # noqa: BLE001 — a repo without a resolvable remote is skipped
                logger.debug("reference_linkifier remote lookup failed for repo %r: %s", repo, exc)
                continue
            slug = git_remote.slug_from_remote(remote)
            if slug:
                return slug, git_remote.web_base_from_remote(remote)
        return "", ""

    def resolve_mr(self, iid: int, *, slug: str = "") -> str | None:
        """Resolve a ``!N`` (or cross-repo) merge/pull-request ref to a URL or ``None``."""
        target_slug = slug or self._default_slug
        db_url = _db_pull_request_url(iid, slug=target_slug)
        if db_url:
            return db_url
        return self._construct(iid, slug=target_slug, is_mr=True)

    def resolve_issue(self, iid: int, *, slug: str = "") -> str | None:
        """Resolve a ``#N`` (or ``owner/repo#N``) issue ref to a URL or ``None``."""
        target_slug = slug or self._default_slug
        db_url = _db_pull_request_url(iid, slug=target_slug) or _db_issue_url(iid, slug=target_slug)
        if db_url:
            return db_url
        return self._construct(iid, slug=target_slug, is_mr=False)

    def resolve_issue_for_slug(self, slug: str, iid: int) -> str | None:
        """Resolve an explicit ``owner/repo#N`` cross-repo issue ref to a URL or ``None``.

        The ``(slug, iid)`` adapter the ``owner/repo#N`` linkify pattern needs.
        The slug from the token always wins over the resolver's default repo —
        a cross-repo ref must resolve against the repo it names, never the
        active one.
        """
        return self.resolve_issue(iid, slug=slug)

    def _construct(self, iid: int, *, slug: str, is_mr: bool) -> str | None:
        """Build a canonical URL from host + slug, or ``None`` when context is missing."""
        if not slug or not self._web_base:
            return None
        base = f"{self._web_base}/{slug}"
        if self._code_host == _GITLAB_HOST:
            tail = "merge_requests" if is_mr else "issues"
            return f"{base}/-/{tail}/{iid}"
        if self._code_host == _GITHUB_HOST:
            # GitHub has no ``!N``; issues<->pull redirect makes ``/issues/N``
            # correct for both an issue and a PR number.
            return f"{base}/issues/{iid}"
        return None


def _db_pull_request_url(iid: int, *, slug: str) -> str | None:
    """Return a stored ``PullRequest`` web URL for ``(slug, iid)`` or ``None``.

    The canonical number-keyed ref->URL store. Matching on the repo slug as
    well as the iid disambiguates the same number across repos. A DB error
    (locked SQLite, app not ready) degrades to ``None`` — never raises onto
    the publish path.
    """
    if not slug:
        return None
    try:
        from teatree.core.models import PullRequest  # noqa: PLC0415 — deferred: ORM import needs the app registry

        row = PullRequest.objects.filter(repo=slug, iid=str(iid)).order_by("-id").first()
    except Exception as exc:  # noqa: BLE001 — DB lookup is best-effort; degrade to construction
        logger.debug("reference_linkifier PullRequest lookup failed for %s#%s: %s", slug, iid, exc)
        return None
    return row.url if row and row.url else None


def _db_issue_url(iid: int, *, slug: str) -> str | None:
    """Return a stored ``Ticket.issue_url`` whose number is ``iid`` on ``slug`` or ``None``.

    Tickets store the full ``issue_url`` (e.g. ``.../issues/42``); a row
    matches when its URL ends with the issue number AND carries the slug, so a
    cross-repo collision on a bare number cannot resolve to the wrong ticket.
    """
    if not slug:
        return None
    try:
        from teatree.core.models import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

        candidates = Ticket.objects.filter(issue_url__contains=f"/{slug}/").exclude(issue_url="")
        for ticket in candidates:
            if _issue_number_of(ticket.issue_url) == iid:
                return ticket.issue_url
    except Exception as exc:  # noqa: BLE001 — DB lookup is best-effort
        logger.debug("reference_linkifier Ticket lookup failed for %s#%s: %s", slug, iid, exc)
    return None


_TRAILING_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"(\d+)/?$")


def _issue_number_of(issue_url: str) -> int | None:
    match = _TRAILING_NUMBER_RE.search(issue_url)
    return int(match.group(1)) if match else None


def linkify(
    text: str,
    *,
    mr_resolver: TokenResolver | None = None,
    issue_resolver: TokenResolver | None = None,
    slug_issue_resolver: "Callable[[str, int], str | None] | None" = None,
) -> str:
    """Return ``text`` with bare refs rewritten to markdown ``[ref](url)``.

    Detects and rewrites:

    - ``!N`` via *mr_resolver* (merge/pull request),
    - ``#N`` via *issue_resolver*,
    - ``owner/repo#N`` via *slug_issue_resolver* (cross-repo issue).

    A resolver returning ``None`` leaves that ref untouched (the gate's
    fallback). Refs inside a markdown link, an inline code span, or a fenced
    code block are skipped. Idempotent: an already-linked ref is protected
    before matching, so a second application is a no-op.
    """
    if not text:
        return text

    protected: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00{len(protected) - 1}\x00"

    # Order matters: stash verbatim/linked spans first so a ref inside them is
    # opaque to the token resolvers below.
    text = _FENCED_BLOCK_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)
    text = _MD_LINK_RE.sub(_stash, text)

    if slug_issue_resolver is not None:
        text = _SLUG_ISSUE_RE.sub(_make_slug_rewriter(slug_issue_resolver), text)
    if mr_resolver is not None:
        text = _BARE_MR_RE.sub(_make_token_rewriter("!", mr_resolver), text)
    if issue_resolver is not None:
        text = _BARE_ISSUE_RE.sub(_make_token_rewriter("#", issue_resolver), text)

    def _restore(match: re.Match[str]) -> str:
        return protected[int(match.group(1))]

    return re.sub(r"\x00(\d+)\x00", _restore, text)


def linkify_with_overlay(text: str, overlay: "OverlayBase | None" = None) -> str:
    """Linkify ``text`` to markdown links using the active overlay's resolvers.

    The production entry point for the markdown surface (forge-agnostic
    bodies). Wires the overlay's ``resolve_mr_token`` / ``resolve_issue_token``
    (which default to the DB+construction :class:`ReferenceResolver`) plus a
    cross-repo ``owner/repo#N`` resolver. Best-effort: a resolver failure
    leaves the ref bare and never crashes the publish path.
    """
    if not text:
        return text
    if overlay is None:
        try:
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415 — deferred: call-time import, kept lazy

            overlay = get_overlay()
        except Exception:  # noqa: BLE001 — overlay resolution is best-effort
            return text
    resolver = ReferenceResolver.from_overlay(overlay)
    return linkify(
        text,
        mr_resolver=overlay.resolve_mr_token,
        issue_resolver=overlay.resolve_issue_token,
        slug_issue_resolver=resolver.resolve_issue_for_slug,
    )


def _make_token_rewriter(sigil: str, resolver: TokenResolver) -> Callable[[re.Match[str]], str]:
    def _rewrite(match: re.Match[str]) -> str:
        n = int(match.group(1))
        url = resolver(n)
        if not url:
            return f"{sigil}{n}"
        return f"[{sigil}{n}]({url})"

    return _rewrite


def _make_slug_rewriter(resolver: "Callable[[str, int], str | None]") -> Callable[[re.Match[str]], str]:
    def _rewrite(match: re.Match[str]) -> str:
        slug, n = match.group(1), int(match.group(2))
        url = resolver(slug, n)
        if not url:
            return f"{slug}#{n}"
        return f"[{slug}#{n}]({url})"

    return _rewrite


__all__ = [
    "ReferenceResolver",
    "TokenResolver",
    "linkify",
    "linkify_with_overlay",
]
