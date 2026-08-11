"""Resolve the ``owner/repo`` a CLEAR's PR/MR lives in.

Maps a ``MergeClear`` (whose ``slug`` is a workstream slug, not a repo) to the
real ``owner/repo`` the merge transport must target, with the #1335 cross-repo
recovery probe. The FORGE hosting that repo is the sibling question, answered by
:mod:`host_kind`. Depends DOWN on :mod:`ci_rollup` for the live-head fetch so
this module and :mod:`execution` both layer above ``ci_rollup`` (no cycle).
"""

import logging
from urllib.parse import urlparse

from teatree.config import discover_overlays
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.merge.head_read_diagnosis import landed_merge_commit, unreadable_head_advisory
from teatree.core.overlay_loader import get_all_overlays
from teatree.project import find_project_root
from teatree.utils import git, git_remote
from teatree.utils.pr_ref import PrRef
from teatree.utils.throttled_log import warn_throttled
from teatree.utils.url_slug import slug_from_issue_or_pr_url

logger = logging.getLogger(__name__)


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


def fallback_repo_slug(clear: object) -> str:
    """The repo *clear*'s TICKET — then the running clone — binds it to, or ``""``.

    Steps (2) and (3) of :func:`resolve_pr_repo_slug`, exposed as a seam so a caller
    that has already judged the CLEAR's own slug unusable as a repo claim resolves
    the remainder in the same order instead of re-deriving it (#4249).
    """
    return _ticket_repo_slug(clear) or _project_repo_slug()


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
    resolved = fallback_repo_slug(clear)
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


def resolved_repo_slug(clear: object) -> str:
    """The real ``owner/repo`` for *clear*'s PR, or ``""`` when unresolvable.

    The non-raising sibling of :func:`resolve_pr_repo_slug`, promoted here as the
    single canonical helper every repo-scoping join site reads (the merged-audit
    checking gather and the waiting-lane covering-CLEAR match). A CLEAR whose repo
    cannot be resolved (a workstream slug with no ticket ``issue_url`` and no
    clone origin) yields ``""`` so a repo-scoping caller drops it, instead of
    surfacing the fail-closed :class:`MergePreconditionError`.
    """
    try:
        return resolve_pr_repo_slug(clear)
    except MergePreconditionError:
        return ""


def normalize_repo_slug(value: str) -> str:
    """Canonicalize *value* UP to a GitHub ``owner/repo`` slug, or ``""``.

    The single normalization boundary for a declared working-repo (#2323):
    :meth:`OverlayReview.merge_candidate_repo_slugs` may return a bare
    ``owner/repo``, an HTTPS URL, an SSH URL, or a ``host-alias`` SSH form
    (``git@github.com-myalias:owner/repo.git``). Each is canonicalized up to
    ``owner/repo`` here so the candidate set holds one consistent
    fully-qualified key — never an under-qualified form matched by stripping
    the registered slug down.

    Delegates to :func:`teatree.utils.git_remote.slug_from_remote`, the pure string
    parser that strips the host prefix from a bare ``owner/repo`` (no-op), an
    HTTPS/SSH URL, and a ``host-alias`` SSH URL, dropping any trailing ``.git``.
    A value that yields no ``owner/repo`` shape (empty, a single path segment,
    an unparsable string) returns ``""`` so the caller drops it.
    """
    slug = git_remote.slug_from_remote(value)
    return slug if _looks_like_owner_repo(slug) else ""


def _overlay_package_repo_slugs() -> list[str]:
    """The ``origin`` slug of every registered overlay's ``project_path``.

    Source (2) for :func:`_iter_candidate_repo_slugs`. Best-effort: a
    ``discover_overlays`` failure yields nothing, and a project path with no
    resolvable ``origin`` remote is skipped — neither blocks the probe.
    """
    try:
        entries = discover_overlays()
    except Exception:  # noqa: BLE001 — best-effort: never block the recovery probe on a registry read
        # A persistently-failing overlay discovery is a real registry fault, not
        # an expected miss — surface it (throttled) instead of silently blanking
        # the candidate set on every S1/S4 compute and merge probe.
        warn_throttled(logger, "slug-probe-discover", "overlay discovery failed during merge probe", exc_info=True)
        return []
    slugs: list[str] = []
    for entry in entries:
        path = getattr(entry, "project_path", None)
        if path is None:
            continue
        try:
            slug = git.remote_slug(repo=str(path))
        except Exception:  # noqa: BLE001 — a missing remote is an expected miss; must not block the probe
            slug = ""
        if slug:
            slugs.append(slug)
    return slugs


def _overlay_working_repo_slugs() -> list[str]:
    """Every overlay's declared working-repos, normalized to ``owner/repo`` (#2323).

    Source (3) for :func:`_iter_candidate_repo_slugs`. Reads each registered
    overlay's :meth:`OverlayReview.merge_candidate_repo_slugs` — repos the
    overlay operates on but does not package (e.g. an ``e2e`` companion repo) —
    and normalizes each declaration up to ``owner/repo`` via
    :func:`normalize_repo_slug`. Best-effort per-overlay: a hook that raises is
    logged and skipped so one broken overlay cannot poison the candidate set.
    """
    try:
        overlays = get_all_overlays()
    except Exception:  # noqa: BLE001 — best-effort: never block the recovery probe on a registry read
        # A persistently-failing overlay load is a real registry fault, not an
        # expected miss — surface it (throttled) rather than silently blanking the
        # working-repo candidate set.
        warn_throttled(logger, "slug-probe-overlays", "overlay load failed during merge probe", exc_info=True)
        return []
    slugs: list[str] = []
    for name, overlay in overlays.items():
        try:
            declared = overlay.review.merge_candidate_repo_slugs()
        except Exception:
            logger.warning(
                "overlay %r review.merge_candidate_repo_slugs() failed during merge probe", name, exc_info=True
            )
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

    Used by :func:`_probe_candidate_heads` to recover from the #1335
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


def known_repo_slugs() -> frozenset[str]:
    """Every ``owner/repo`` this machine's overlay registry can name — forge-free (#4249).

    The set form of :func:`_iter_candidate_repo_slugs`, promoted as the shared
    evidence for "does this slug name a repo that EXISTS here?" — the question
    :func:`_looks_like_owner_repo` can only answer from string shape.
    """
    return frozenset(_iter_candidate_repo_slugs())


def slug_is_registered_repo(slug: str) -> bool:
    """True iff *slug* canonicalizes to a repo :func:`known_repo_slugs` names (#4249).

    Positive evidence only: ``False`` covers BOTH "the registry names other repos
    and not this one" AND "the registry names nothing at all", so a caller that
    must fail open on an empty registry tests :func:`known_repo_slugs` itself
    rather than reading a bare ``False`` as a denial.
    """
    canonical = normalize_repo_slug(slug)
    return bool(canonical) and canonical in known_repo_slugs()


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

    A no-match raise is classified before it is composed (#4239, #4144). A forge
    read that returns nothing is a NON-answer, so "the head moved" is a claim the
    gate has no evidence for. When the INITIAL read came back empty, two things
    follow: the PR may simply have merged already — checked first, whatever
    emptied the read (#4144; a merged PR's head going unreadable is not shown to
    be caused by its branch being deleted — GitHub keeps answering ``headRefOid``
    for merged PRs after their source branch is gone, so a transient forge error
    is at least as likely) — and, failing that, the refusal says the head could
    not be READ rather than that it moved, naming the venue's missing credential
    or (when candidates DID answer) the cross-repo hypothesis those answers
    actually support. Only a head that RESOLVES to a different SHA keeps the
    head-moved diagnosis.
    """
    if not reviewed_sha:
        return initial_slug
    query = CodeHostQuery.for_ref(PrRef(slug=initial_slug, pr_id=pr_id, host_kind=host_kind))
    initial_live = query.live_head_sha()
    if initial_live == reviewed_sha:
        return initial_slug
    # An empty ``initial_live`` can be a #1335 signal rather than an auth/network
    # failure: a cross-repo CLEAR resolves to the running clone's ``origin`` (the
    # wrong repo), which has no PR <pr_id> at all, so the forge returns an empty
    # head for it. Fall through to the cross-repo probe either way — a candidate
    # overlay repo whose PR <pr_id> carries ``reviewed_sha`` is recovered, and the
    # per-candidate heads are what tell the two causes apart when none matches.
    candidates = _iter_candidate_repo_slugs()
    # The initial slug was already probed above — exclude it from the secondary
    # set so the candidates list in the error message reflects what was probed.
    other_candidates = [c for c in candidates if c != initial_slug]
    probed_heads = _probe_candidate_heads(query=query, candidates=other_candidates)
    matches = [slug for slug, head in probed_heads.items() if head == reviewed_sha]
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
    if not initial_live:
        if landed := landed_merge_commit(query):
            logger.info(
                "merge_execution: PR #%s on %r reads MERGED at %s — its unreadable head is the "
                "expected post-merge state, not drift (#4144)",
                pr_id,
                initial_slug,
                landed[:8],
            )
            return initial_slug
        readable = sorted(slug for slug, head in probed_heads.items() if head)
        cause = (
            unreadable_head_advisory(host_kind)
            if not readable
            else (
                f"The forge DID answer for {readable}, so a missing credential is not the cause — "
                f"the initial repo most likely carries no PR #{pr_id} at all (a CLEAR issued from a "
                f"clone whose overlay registry doesn't include the target repo). This refusal "
                f"consumes nothing — the CLEAR stays actionable."
            )
        )
        msg = (
            f"could not read the live head for PR #{pr_id} on {initial_slug!r}, so it was never "
            f"compared with the reviewed SHA {reviewed_sha} — this is NOT evidence that the head "
            f"moved, and no candidate repo's PR #{pr_id} carries that SHA either. Candidates "
            f"considered: {considered}. {cause} Re-escalate; the loop never self-issues a "
            f"replacement (§17.4.3 step 2 / #4239)."
        )
        raise MergePreconditionError(msg)
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


def _probe_candidate_heads(
    *,
    query: CodeHostQuery,
    candidates: list[str],
) -> dict[str, str]:
    """Each candidate ``owner/repo`` mapped to its live PR <pr_id> head SHA (#2338, #4239).

    Probes **all** candidates in order — the #1335 recovery path enumerates the
    repos that could own the reviewed work, and the caller requires EXACTLY ONE
    to carry the reviewed SHA. Reporting every candidate (not stopping at the
    first match) is what lets the caller detect a same-SHA ambiguity: when two
    distinct candidate repos (a fork/mirror, or an overlay working-repo that
    aliases another) both expose PR <pr_id> at the same reviewed SHA, binding
    silently to whichever was probed first would merge an unverified twin.

    Reporting the HEAD rather than a match verdict is what lets the caller tell a
    forge that answered "a different SHA" from one that did not answer at all
    (#4239) — the two need opposite diagnoses and are indistinguishable once
    collapsed to a boolean.

    Reuses *query*'s already-resolved backend (:meth:`CodeHostQuery.rebound_to`),
    re-reading ``pulls/<N>`` per candidate slug without re-resolving the transport.
    The per-candidate swallow-failures contract is preserved: a probe error
    surfaces as an empty head from :meth:`CodeHostQuery.live_head_sha`, which never
    equals a reviewed SHA, so a failing candidate can never be selected — and
    never raises on its own.
    """
    return {slug: query.rebound_to(slug).live_head_sha() for slug in candidates}
