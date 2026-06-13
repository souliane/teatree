"""Resolve the GitHub/GitLab ``owner/repo`` + host kind for a CLEAR's PR.

Maps a ``MergeClear`` (whose ``slug`` is a workstream slug, not a repo) to the
real ``owner/repo`` the merge transport must target, with the #1335 cross-repo
recovery probe. Depends DOWN on :mod:`ci_rollup` for the live-head fetch so this
module and :mod:`execution` both layer above ``ci_rollup`` (no cycle).
"""

import logging
from urllib.parse import urlparse

from teatree.config import discover_overlays
from teatree.core.merge.ci_rollup import fetch_live_head_sha
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.overlay_loader import get_all_overlays
from teatree.project import find_project_root
from teatree.utils import git
from teatree.utils.url_slug import slug_from_issue_or_pr_url

logger = logging.getLogger(__name__)


def _resolve_host_kind(clear: object) -> str:
    """Return ``"github"`` or ``"gitlab"`` for *clear*'s PR transport.

    Resolution order:

    (1) the CLEAR's ``ticket.issue_url`` — ``github.com`` → ``"github"``,
        any URL whose hostname contains ``gitlab`` (gitlab.com or a
        self-hosted ``gitlab.<corp>`` host) → ``"gitlab"``.
    (2) default ``"github"`` — back-compat for CLEAR rows without a
        ticket / without a recognisable ``issue_url``. Pre-existing
        GitHub callers keep the legacy ``gh`` transport unchanged.

    The host kind is a transport-only switch; every §17.4.3 guard
    (substrate refusal, reviewer≠loop, SHA-bind, single-use replay)
    is identical across forges.
    """
    ticket = getattr(clear, "ticket", None)
    issue_url = str(getattr(ticket, "issue_url", "") or "") if ticket is not None else ""
    if not issue_url:
        return "github"
    host = urlparse(issue_url).hostname or ""
    host = host.lower()
    if "github.com" in host or host == "github":
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return "github"


_GIT_BRANCH_PREFIXES = frozenset(
    {
        "fix",
        "feat",
        "feature",
        "chore",
        "docs",
        "bugfix",
        "hotfix",
        "release",
        "refactor",
        "test",
        "ci",
        "build",
        "perf",
        "style",
        # Personal-workflow prefixes the user's branches actually carry.
        # ``ac/`` is the user's initials; ``wip/``, ``dev/``, and ``tmp/``
        # are common scratch / iteration namespaces. They are NOT GitHub
        # owners, so any CLEAR slug whose first segment matches must fall
        # through to the ticket-issue-url / clone-origin fallbacks (#1005).
        # Keep these — they're load-bearing for the user's day-to-day
        # merges, not "non-standard" prefixes to strip.
        "ac",
        "wip",
        "dev",
        "tmp",
    }
)


def _looks_like_owner_repo(slug: str) -> bool:
    """True when *slug* is already a GitHub ``owner/repo`` identifier.

    A workstream slug (``statusline-stale-wakeup``) has no ``/``; a repo
    slug (``souliane/teatree``) has exactly one path separator and is not
    a filesystem path.

    A *branch-shaped* slug (``fix/review-cli-django-bootstrap``,
    ``ac/cli-bundle-…``) also has exactly one ``/`` and would otherwise
    pass the structural check — yet it is a git branch name, not an
    ``owner/repo`` (#1005). Such a slug must fall through to the
    ticket-issue-url and clone-origin fallbacks so the real repo is
    resolved. A real GitHub owner cannot be one of the standard git
    branch namespaces (``fix``, ``feat``, ``chore``, …) nor the user's
    personal-workflow prefixes (``ac``, ``wip``, ``dev``, ``tmp``), so
    any slug whose first path segment is in
    :data:`_GIT_BRANCH_PREFIXES` (case-insensitive) is rejected here.
    The alternative — re-ordering :func:`resolve_pr_repo_slug` to consult
    the ticket/clone fallbacks before the structural check — would change
    the documented resolution order and weaken back-compat with rows that
    deliberately store an ``owner/repo`` slug; tightening this predicate
    is a smaller-surface fix.
    """
    if not ("/" in slug and not slug.startswith("/") and ":" not in slug and slug.count("/") >= 1):
        return False
    first_segment = slug.split("/", 1)[0]
    return first_segment.lower() not in _GIT_BRANCH_PREFIXES


def _project_repo_slug() -> str:
    """The GitHub ``owner/repo`` for the running teatree clone, or ``""``.

    Resolved from the project root's ``origin`` git remote — the same
    canonical :func:`git.remote_slug` path ``_ensure_pr.py`` /
    ``backends.github`` use to target ``gh`` at the right repo.
    """
    root = find_project_root()
    if root is None:
        return ""
    return git.remote_slug(repo=str(root))


def _ticket_repo_slug(clear: object) -> str:
    """The GitHub ``owner/repo`` for *clear*'s ticket, or ``""`` (#931).

    Resolved from the CLEAR's ``ticket.issue_url`` via the canonical
    :func:`slug_from_issue_or_pr_url` parser — the repo the PR genuinely
    belongs to, independent of which clone is running. This is the
    authoritative source when an overlay's GitHub repo differs from the
    editable ``t3`` clone's ``origin``: such a CLEAR must bind its
    live-head check to the overlay repo's PR, never to a same-numbered
    PR in the clone-origin repo (#931).
    """
    ticket = getattr(clear, "ticket", None)
    if ticket is None:
        return ""
    issue_url = str(getattr(ticket, "issue_url", "") or "")
    if not issue_url:
        return ""
    return slug_from_issue_or_pr_url(urlparse(issue_url).path)


def resolve_pr_repo_slug(clear: object) -> str:
    """The GitHub ``owner/repo`` to target ``gh`` at for *clear*'s PR.

    ``MergeClear.slug`` is a *workstream* slug, not a repo. Resolution
    order, first non-empty wins:

    (1) an ``owner/repo``-shaped slug is used as-is (back-compat with
    rows / tests that stored a repo there).
    (2) the CLEAR's ``ticket.issue_url`` repo (#931 — authoritative: the
    repo the PR belongs to, correct even when the overlay's repo differs
    from the running clone's ``origin``).
    (3) the running clone's ``origin`` git remote (the teatree-self
    overlay, whose repo *is* the clone origin).

    Fails closed with an actionable :class:`MergePreconditionError` when
    none yields a repo — never the opaque "could not resolve the live
    head" escalation that hid this gap.
    """
    slug = str(getattr(clear, "slug", "") or "")
    pr_id = getattr(clear, "pr_id", "?")
    if _looks_like_owner_repo(slug):
        return slug
    from_ticket = _ticket_repo_slug(clear)
    if from_ticket:
        return from_ticket
    resolved = _project_repo_slug()
    if resolved:
        return resolved
    msg = (
        f"could not resolve the GitHub repo for {slug}#{pr_id}: the CLEAR slug "
        f"{slug!r} is a workstream slug (not owner/repo), the CLEAR's ticket has "
        f"no recognisable GitHub issue_url, and the running teatree clone has no "
        f"resolvable 'origin' remote. The sanctioned merge needs the real repo to "
        f"bind the merge — re-issue the CLEAR from a checkout whose 'origin' points "
        f"at the GitHub repo, or pass an owner/repo slug."
    )
    raise MergePreconditionError(msg)


def normalize_repo_slug(value: str) -> str:
    """Canonicalize *value* UP to a GitHub ``owner/repo`` slug, or ``""``.

    The single normalization boundary for a declared working-repo (#2323):
    :meth:`OverlayBase.get_merge_candidate_repo_slugs` may return a bare
    ``owner/repo``, an HTTPS URL, an SSH URL, or a ``host-alias`` SSH form
    (``git@github.com-myalias:owner/repo.git``). Each is canonicalized up to
    ``owner/repo`` here so the candidate set holds one consistent
    fully-qualified key — never an under-qualified form matched by stripping
    the registered slug down.

    Delegates to :func:`teatree.utils.git.slug_from_remote`, the pure string
    parser that strips the host prefix from a bare ``owner/repo`` (no-op), an
    HTTPS/SSH URL, and a ``host-alias`` SSH URL, dropping any trailing ``.git``.
    A value that yields no ``owner/repo`` shape (empty, a single path segment,
    an unparsable string) returns ``""`` so the caller drops it.
    """
    slug = git.slug_from_remote(value)
    return slug if _looks_like_owner_repo(slug) else ""


def _overlay_package_repo_slugs() -> list[str]:
    """The ``origin`` slug of every registered overlay's ``project_path``.

    Source (2) for :func:`_iter_candidate_repo_slugs`. Best-effort: a
    ``discover_overlays`` failure yields nothing, and a project path with no
    resolvable ``origin`` remote is skipped — neither blocks the probe.
    """
    try:
        entries = discover_overlays()
    except Exception:  # noqa: BLE001 — overlay discovery is best-effort here
        return []
    slugs: list[str] = []
    for entry in entries:
        path = getattr(entry, "project_path", None)
        if path is None:
            continue
        try:
            slug = git.remote_slug(repo=str(path))
        except Exception:  # noqa: BLE001 — a missing remote must not block the probe
            slug = ""
        if slug:
            slugs.append(slug)
    return slugs


def _overlay_working_repo_slugs() -> list[str]:
    """Every overlay's declared working-repos, normalized to ``owner/repo`` (#2323).

    Source (3) for :func:`_iter_candidate_repo_slugs`. Reads each registered
    overlay's :meth:`OverlayBase.get_merge_candidate_repo_slugs` — repos the
    overlay operates on but does not package (e.g. an ``e2e`` companion repo) —
    and normalizes each declaration up to ``owner/repo`` via
    :func:`normalize_repo_slug`. Best-effort per-overlay: a hook that raises is
    logged and skipped so one broken overlay cannot poison the candidate set.
    """
    try:
        overlays = get_all_overlays()
    except Exception:  # noqa: BLE001 — overlay instantiation is best-effort here
        return []
    slugs: list[str] = []
    for name, overlay in overlays.items():
        try:
            declared = overlay.get_merge_candidate_repo_slugs()
        except Exception:
            logger.warning("overlay %r get_merge_candidate_repo_slugs() failed during merge probe", name, exc_info=True)
            continue
        slugs.extend(normalize_repo_slug(raw) for raw in declared)
    return slugs


def _iter_candidate_repo_slugs() -> list[str]:
    """Every ``owner/repo`` reachable from this machine's overlay registry (#1335, #2323).

    Source set, de-duplicated preserving insertion order:

    (1) the running clone's ``origin`` (the same value
        :func:`_project_repo_slug` returns).
    (2) the ``origin`` slug of every registered overlay's ``project_path``
        (entry-point + TOML overlays via :func:`_overlay_package_repo_slugs`).
    (3) each registered overlay's declared **working-repos**
        (:func:`_overlay_working_repo_slugs`) — repos the overlay operates on but
        does not package. A CLEAR for a PR in one of them (e.g. an ``e2e``
        companion repo) was previously unmergeable because the candidate set
        never contained it (#2323).

    Used by :func:`_probe_candidate_repos` to recover from the #1335
    cross-repo confusion: a CLEAR issued from the teatree clone for a PR
    in a downstream overlay's repo (e.g. ``downstream-org/downstream-overlay#159``)
    used to resolve to ``souliane/teatree``'s same-numbered (unrelated)
    PR. With this enumeration the probe can verify each candidate and
    pick the one whose ``pulls/<N>`` head matches the reviewed SHA.

    Probe-side failures (a source helper raises, a project path has no
    ``origin`` remote, an overlay's working-repo hook raises) are swallowed:
    the candidate set is best-effort, never load-bearing for the happy path.
    """
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(slug: str) -> None:
        if slug and slug not in seen:
            seen.add(slug)
            candidates.append(slug)

    _add(_project_repo_slug())
    for slug in _overlay_package_repo_slugs():
        _add(slug)
    for slug in _overlay_working_repo_slugs():
        _add(slug)

    return candidates


def _reconcile_slug_against_reviewed_sha(
    *,
    initial_slug: str,
    pr_id: int,
    reviewed_sha: str,
    host_kind: str,
) -> str:
    """Pick the right repo when *initial_slug*'s PR doesn't carry *reviewed_sha* (#1335).

    The initial slug is what :func:`resolve_pr_repo_slug` returned: an
    explicit ``owner/repo`` from the CLEAR, the ticket's ``issue_url``
    repo, or — the #1335 trap — the running clone's ``origin`` for a
    CLEAR with no ticket and a non-``owner/repo`` slug. When that initial
    slug's PR head SHA matches *reviewed_sha* the merge proceeds against
    it unchanged (the common path). When the SHAs disagree, the same
    PR number may live in a downstream overlay's repo at the right SHA;
    the probe enumerates :func:`_iter_candidate_repo_slugs` and recovers
    the candidate whose ``pulls/<N>`` head matches — requiring EXACTLY ONE.

    No reviewed SHA, no probe (back-compat with legacy callers that did
    not carry the SHA). No candidate match raises a
    :class:`MergePreconditionError` whose message names every candidate
    considered so the diagnosis is unambiguous — never the opaque "head
    moved" escalation that hid the #1335 bug.

    More than one candidate matching *reviewed_sha* is the #2338 same-SHA
    ambiguity: two distinct repos (a fork/mirror, or an overlay working-repo
    aliasing another) both expose PR <pr_id> at the reviewed SHA, so binding
    to whichever was probed first would merge an unverified twin. That case
    raises a :class:`MergePreconditionError` naming every ambiguous repo —
    the gate never silently picks one.
    """
    if not reviewed_sha:
        return initial_slug
    initial_live = fetch_live_head_sha(initial_slug, pr_id, host_kind=host_kind)
    if initial_live == reviewed_sha:
        return initial_slug
    # An empty ``initial_live`` is itself a #1335 signal, NOT merely a transient
    # auth/network failure: a cross-repo CLEAR resolves to the running clone's
    # ``origin`` (the wrong repo), which has no PR <pr_id> at all, so the forge
    # returns an empty head for it. Fall through to the cross-repo probe so a
    # candidate overlay repo whose PR <pr_id> carries ``reviewed_sha`` is
    # recovered. The genuinely-absent case (no candidate matches) is still
    # covered by the ``MergePreconditionError`` below, whose message names every
    # candidate considered — so a real auth/network outage still fails loud.
    candidates = _iter_candidate_repo_slugs()
    # The initial slug was already probed above — exclude it from the secondary
    # set so the candidates list in the error message reflects what was probed.
    other_candidates = [c for c in candidates if c != initial_slug]
    matches = _probe_candidate_repos(
        pr_id=pr_id,
        reviewed_sha=reviewed_sha,
        candidates=other_candidates,
        host_kind=host_kind,
    )
    if len(matches) > 1:
        # #2338: a same-SHA multi-match is an ambiguity the merge gate must
        # never resolve silently — binding to ``matches[0]`` could merge an
        # unverified fork/mirror twin. Fail loud, naming every ambiguous repo.
        msg = (
            f"ambiguous merge candidate for PR #{pr_id}: {len(matches)} distinct "
            f"repos expose PR #{pr_id} at the reviewed SHA {reviewed_sha} — "
            f"{matches}. The merge gate refuses to pick one silently (a fork / "
            f"mirror, or an overlay working-repo aliasing another, could shadow "
            f"the reviewed work). Re-issue the CLEAR with an explicit owner/repo "
            f"slug naming the intended repo (§17.4.3 step 2 / #2338)."
        )
        raise MergePreconditionError(msg)
    if matches:
        match = matches[0]
        logger.info(
            "merge_execution: cross-repo recovery for #%s — initial slug %r "
            "live=%s != reviewed=%s; probed %s, matched %r",
            pr_id,
            initial_slug,
            initial_live or "(unresolved)",
            reviewed_sha,
            other_candidates,
            match,
        )
        return match
    considered = [initial_slug, *other_candidates]
    msg = (
        f"PR head moved: live={initial_live or '(unresolved)'} != "
        f"reviewed={reviewed_sha} on the initial repo ({initial_slug!r}), and "
        f"no other candidate repo's PR #{pr_id} carries that SHA either. "
        f"Candidates considered: {considered}. This is either a genuine "
        f"force-push / new commits on the PR, or the CLEAR was issued from a "
        f"clone whose overlay registry doesn't include the target repo. "
        f"Re-escalate; the loop never self-issues a replacement "
        f"(§17.4.3 step 2 / #1335)."
    )
    raise MergePreconditionError(msg)


def _probe_candidate_repos(
    *,
    pr_id: int,
    reviewed_sha: str,
    candidates: list[str],
    host_kind: str,
) -> list[str]:
    """Every candidate ``owner/repo`` whose PR <pr_id> head == *reviewed_sha* (#2338).

    Probes **all** candidates and returns the full list of those whose live
    head SHA matches *reviewed_sha* — the #1335 recovery path enumerates the
    repos that could own the reviewed work, and the caller requires EXACTLY
    ONE to match. Returning every match (not just the first) is what lets the
    caller detect a same-SHA ambiguity: when two distinct candidate repos
    (a fork/mirror, or an overlay working-repo that aliases another) both
    expose PR <pr_id> at the same reviewed SHA, binding silently to whichever
    was probed first would merge an unverified twin. The list lets the caller
    raise instead, naming every ambiguous repo.

    The per-candidate swallow-failures contract is preserved: a probe error
    surfaces as an empty head from :func:`fetch_live_head_sha`, which never
    equals *reviewed_sha*, so a failing candidate is simply absent from the
    matches — never counted, never raising on its own. Returns ``[]`` when no
    candidate matches (a real force-push or a truly stale CLEAR).
    """
    return [slug for slug in candidates if fetch_live_head_sha(slug, pr_id, host_kind=host_kind) == reviewed_sha]
