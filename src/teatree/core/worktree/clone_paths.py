"""Source-clone resolution shared by provisioning, cleanup, orphan-guard, reconcile.

Lives in ``teatree.core`` rather than ``teatree.core.runners`` because importing
``runners`` triggers ``runners.__init__`` which pulls in ``cleanup`` (via
``teardown``) — a circular import the moment ``cleanup`` itself wants to
resolve a clone.
"""

import logging
from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout
from teatree.utils import git

logger = logging.getLogger(__name__)


def find_clone_path(workspace: Path, repo_name: str) -> Path | None:
    """Resolve ``repo_name`` to an actual git clone under ``workspace``.

    Tries the literal path first (``workspace / repo_name``) so explicit
    ``souliane/teatree``-style entries keep working. If that's not a git
    checkout, scans one level deep — ``workspace / */basename`` — so a bare
    ``teatree`` from ``--repos teatree`` finds the namespaced clone at
    ``workspace/souliane/teatree``. Returns ``None`` when no match exists.
    Logs a warning when more than one match is found and picks the first
    (alphabetic) so the operator can spot basename collisions in the logs.

    A non-existent ``workspace`` resolves to ``None`` (no clone), never a crash:
    the per-overlay ``workspace_dir`` default (``~/workspace/t3-workspaces/<overlay>/``)
    need not exist yet on a fresh setup, and ``iterdir()`` would raise
    ``FileNotFoundError`` on the one-level scan otherwise.
    """
    literal = workspace / repo_name
    if (literal / ".git").is_dir():
        return literal

    if not workspace.is_dir():
        return None

    basename = Path(repo_name).name
    matches: list[Path] = []
    for entry in sorted(workspace.iterdir()):
        if not entry.is_dir() or entry == literal:
            continue
        candidate = entry / basename
        if (candidate / ".git").is_dir():
            matches.append(candidate)

    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Multiple clones match %r under %s; picking %s. Pass --repos with the namespace prefix to disambiguate.",
            repo_name,
            workspace,
            matches[0],
        )
    return matches[0]


def stored_clone_path(worktree: Worktree) -> Path | None:
    """``worktree.extra['clone_path']``, but only while it still resolves as a checkout.

    The recorded path is a claim, not a fact: a deploy that relocates (or a
    hand-moved clone) leaves the row pointing at nothing, and every git probe run
    against that path answers "could not read" — which the redundancy layers then
    render as an empty unique-commit list, indistinguishable from a branch proven
    to hold nothing. Positive proof is required, so an ``INCONCLUSIVE`` probe
    falls through to a fresh scan instead of being trusted.
    """
    stored = (worktree.extra or {}).get("clone_path", "")
    if not stored:
        return None
    return Path(stored) if probe_checkout(Path(stored)) is CheckoutState.CHECKOUT else None


def clone_path_from_checkout(wt_path: str) -> Path | None:
    """The clone backing an on-disk worktree, asked of GIT rather than guessed by name.

    ``git rev-parse --git-common-dir`` answers with the MAIN clone's git directory
    (``<clone>/.git`` from a linked worktree), so its parent is the clone's working
    tree. This is the only authoritative answer available: a worktree created by a
    bare ``git worktree add`` has no recorded ``clone_path`` and its directory
    basename need not match the repo, so every name-based resolution misses it —
    and a miss is not benign. It renders as "source clone missing", which the
    redundancy layers then report as an empty unique-commit list, indistinguishable
    from a branch proven to hold nothing.

    Returns ``None`` when *wt_path* is not a git worktree or the clone it names is
    not a live checkout, so callers fall through to the name scan exactly as before.
    """
    clone = git_common_clone_dir(wt_path)
    if clone is None:
        return None
    return clone if probe_checkout(clone) is CheckoutState.CHECKOUT else None


def git_common_clone_dir(wt_path: str) -> Path | None:
    """The working tree of the main clone *wt_path* belongs to, per ``--git-common-dir``.

    The raw, UNVALIDATED probe — it reports where git says the clone is without
    asking whether that path is currently a healthy checkout. Callers that need the
    stricter answer use :func:`clone_path_from_checkout`; the adopt path in
    provisioning wants the raw one, because it is establishing what the checkout
    belongs to rather than deciding whether to trust it.

    An EMPTY *wt_path* is rejected before anything else: ``Path("")`` is ``.``,
    which is a directory, so a blank path would run ``git`` in the CALLING
    process's cwd and confidently report that repo as the answer — a row with no
    recorded worktree path would resolve to whatever clone the CLI happened to be
    invoked from.
    """
    if not wt_path.strip() or not Path(wt_path).is_dir():
        return None
    common = git.run(repo=wt_path, args=["rev-parse", "--git-common-dir"])
    if not common:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (Path(wt_path) / common_path).resolve()
    return common_path.parent


def resolve_clone_path(workspace: Path, worktree: Worktree) -> Path | None:
    """Return the source clone path for *worktree*, with namespace fallback.

    Three tiers, most authoritative first:

    1. a :func:`stored_clone_path` that is still a live checkout;
    2. :func:`clone_path_from_checkout` — what GIT says the worktree belongs to;
    3. a :func:`find_clone_path` name scan under *workspace*.

    Tier 2 exists because tiers 1 and 3 both go by NAME, and a worktree created by
    a bare ``git worktree add`` satisfies neither: it carries no recorded
    ``clone_path``, and its directory basename is whatever the operator typed, not
    the repo. Such a worktree resolved to ``workspace / <basename>`` — a path that
    does not exist — so teardown reported "source repo missing" and removed
    nothing, and the redundancy probes reported it unverifiable. Git knows the
    answer; asking it is both cheaper and correct.

    ``None`` means no clone exists anywhere — callers read that as unverifiable and
    keep, never as "nothing here to lose".
    """
    stored = stored_clone_path(worktree)
    if stored is not None:
        return stored
    from_git = clone_path_from_checkout((worktree.extra or {}).get("worktree_path", "") or "")
    if from_git is not None:
        return from_git
    return find_clone_path(workspace, worktree.repo_path)


def repair_stale_clone_path(workspace: Path, worktree: Worktree) -> Path | None:
    """Rewrite a stale ``extra['clone_path']`` to the clone that exists; ``None`` when untouched.

    Only ever moves the row toward the truth: a stored path the checkout probe
    confirms is left alone, and a scan that finds nothing leaves the stale value
    in place as a breadcrumb rather than blanking the only record of where the
    clone used to be.
    """
    if stored_clone_path(worktree) is not None:
        return None
    found = find_clone_path(workspace, worktree.repo_path)
    if found is None or str(found) == (worktree.extra or {}).get("clone_path", ""):
        return None
    worktree.extra = {**(worktree.extra or {}), "clone_path": str(found)}
    worktree.save(update_fields=["extra"])
    return found
