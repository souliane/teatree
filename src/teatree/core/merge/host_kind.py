"""Resolve the forge transport (``github`` / ``gitlab``) a CLEAR's merge must use.

A distinct question from :mod:`pr_slug_resolution`, which answers WHICH repo an
``owner/repo`` slug names: this module answers WHICH FORGE hosts it. The two are
independent — a bare ``owner/repo`` slug carries no host — so the forge is
resolved from the CLEAR's own recorded target first and from hard host-bearing
evidence (a ticket's issue URL, the running clone's ``origin`` remote) after.

There is deliberately NO default. Silently picking ``"github"`` for an
unresolvable forge bound the GitHub transport against a GitLab MR and made a
ticketless CLEAR unmergeable while still writing the row — an orphan that
ratchets the S4 stale-CLEAR signal hard-red. An unresolvable forge raises
:class:`MergePreconditionError` here so ``ticket clear`` refuses issuance
BEFORE any row is written, and ``ticket merge`` re-escalates instead of
executing against the wrong forge.
"""

import logging

from teatree.core.intake.repo_scope import host_aware_owns, identity_from_host_and_slug
from teatree.core.merge.errors import MergePreconditionError
from teatree.core.overlay_loader import get_all_overlays
from teatree.project import find_project_root
from teatree.utils import git_remote
from teatree.utils.forge import FORGES, forge_from_host, forge_from_remote, normalize_forge
from teatree.utils.git_remote_ops import remote_url
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)


def _recorded_forge(clear: object) -> str:
    """The forge explicitly recorded on *clear*, or ``""`` when none is set.

    A non-blank value that names no known forge is a typo, not an absent target:
    falling through to the derived steps would bind a forge the caller never
    asked for and quietly discard their instruction, so it raises here.
    """
    raw = str(getattr(clear, "host_kind", "") or "").strip()
    if not raw:
        return ""
    if forge := normalize_forge(raw):
        return forge
    msg = (
        f"unknown forge {raw!r}; valid: {sorted(FORGES)}. It names the forge the merge "
        f"transport binds to, so an unrecognised value is refused rather than derived "
        f"around — that would silently merge on a forge you did not name."
    )
    raise MergePreconditionError(msg)


def _ticket_issue_forge(clear: object) -> str:
    ticket = getattr(clear, "ticket", None)
    if ticket is None:
        return ""
    return forge_from_remote(str(getattr(ticket, "issue_url", "") or ""))


def _running_clone_forge(repo_slug: str) -> str:
    """The forge of the running clone when that clone IS *repo_slug*, else ``""``.

    Identity, not inference: the project root's ``origin`` remote URL carries
    both the slug and the host, so a CLEAR naming the very repo the command runs
    inside resolves its forge from that remote with no guessing. A clone whose
    ``origin`` names a DIFFERENT repo yields nothing — its host says nothing
    about where *repo_slug* lives.
    """
    if not repo_slug:
        return ""
    root = find_project_root()
    if root is None:
        return ""
    origin = remote_url(repo=str(root))
    if git_remote.slug_from_remote(origin).lower() != repo_slug.strip().lower():
        return ""
    return forge_from_remote(origin)


def _declared_scope_hosts(repo_slug: str) -> list[str]:
    """Every registered overlay's declared forge host that owns *repo_slug*'s namespace.

    ``OverlayConfig.owned_repos`` is the forge-host-keyed SCOPE registry
    (BLUEPRINT § "Repo SCOPE axis"): ``{"github.com": ["souliane"]}`` is the
    operator's own statement that the ``souliane`` namespace lives on
    github.com. Reading it here recovers the host a bare ``owner/repo`` slug
    dropped — a declaration, not an inference. Read independently of
    ``require_owned_repo_approval``: that flag arms the approval GATE, while
    this only asks the registry where a namespace is hosted.

    Best-effort: a registry that fails to load yields nothing (throttled warn)
    and the caller falls through to its fail-loud refusal.
    """
    if not repo_slug:
        return []
    try:
        overlays = get_all_overlays()
    except Exception:  # noqa: BLE001 — best-effort evidence: a registry fault must not mask the actionable refusal
        warn_throttled(logger, "host-kind-scope", "overlay load failed while resolving a CLEAR's forge", exc_info=True)
        return []
    return [
        host
        for overlay in overlays.values()
        for host, patterns in (overlay.config.owned_repos or {}).items()
        if host_aware_owns({host: patterns}, identity_from_host_and_slug(host, repo_slug))
    ]


def _declared_scope_forge(repo_slug: str) -> str:
    """The single forge whose declared scope owns *repo_slug*, or ``""``.

    Two distinct forges claiming the same namespace is an ambiguity the merge
    gate refuses rather than resolving silently — binding to whichever overlay
    loaded first could drive the merge at a same-named repo on the wrong forge.
    """
    forges = {forge for forge in map(forge_from_host, _declared_scope_hosts(repo_slug)) if forge}
    if len(forges) > 1:
        msg = (
            f"ambiguous forge for {repo_slug!r}: {sorted(forges)} are both declared to own that "
            f"namespace in an overlay's `owned_repos`. The merge gate refuses to pick one — "
            f"re-issue the CLEAR naming the forge explicitly (`--forge <github|gitlab>`)."
        )
        raise MergePreconditionError(msg)
    return next(iter(forges), "")


def resolve_host_kind(clear: object, *, repo_slug: str = "") -> str:
    """The forge transport for *clear*'s PR/MR — never a default, raises when unknown.

    Resolution order, first non-empty wins:

    (1) the forge recorded ON the CLEAR (``host_kind``) — the explicit target a
    ticketless CLEAR carries (``ticket clear --forge gitlab``), and the value
    every issued CLEAR persists so the orchestrator → loop handoff survives a
    restart without re-deriving anything.
    (2) the CLEAR's ``ticket.issue_url`` host — authoritative when a ticket
    exists, and the sole legacy rule this replaces.
    (3) the running clone's ``origin`` remote, but ONLY when that clone's slug
    equals *repo_slug* (the repo the merge resolved) — see
    :func:`_running_clone_forge`.
    (4) the forge host an overlay's ``owned_repos`` declares for *repo_slug*'s
    namespace — see :func:`_declared_scope_forge`.

    Raises :class:`MergePreconditionError` naming the actionable fix when none
    yields a forge. Accepts both a :class:`~teatree.core.models.ClearRequest`
    (issuance time, before any row exists) and a persisted
    :class:`~teatree.core.models.MergeClear` (merge time), exactly like
    :func:`~teatree.core.merge.pr_slug_resolution.resolve_pr_repo_slug`.
    """
    # Short-circuiting is load-bearing, not an optimization: a later step must not
    # run — nor raise its own ambiguity — once a more authoritative source above
    # it has already named the forge.
    if recorded := _recorded_forge(clear):
        return recorded
    if from_ticket := _ticket_issue_forge(clear):
        return from_ticket
    if from_clone := _running_clone_forge(repo_slug):
        return from_clone
    if declared := _declared_scope_forge(repo_slug):
        return declared
    slug = repo_slug or str(getattr(clear, "slug", "") or "")
    pr_id = getattr(clear, "pr_id", "?")
    msg = (
        f"could not resolve the forge for {slug}#{pr_id}: the CLEAR records no forge, its "
        f"ticket has no recognisable github.com/gitlab issue_url, the running clone's "
        f"'origin' does not point at {slug!r}, and no overlay's `owned_repos` declares a "
        f"forge host for that namespace. A bare owner/repo slug carries no host, and the "
        f"merge gate never guesses one — binding the wrong transport would probe an "
        f"unrelated repo. Re-issue the CLEAR naming the forge explicitly: "
        f"`t3 <overlay> ticket clear {pr_id} {slug} --forge <github|gitlab> ...`"
    )
    raise MergePreconditionError(msg)
