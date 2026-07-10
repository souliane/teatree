"""Django-aware wiring for the :mod:`teatree.core.fleet_claim` mutex.

:mod:`teatree.core.fleet_claim` is deliberately Django-free (a repo path in, a
git CAS out). This thin layer supplies the two things the claim + ship-fence call
sites need from the Django world and nowhere else:

*   the ``fleet_claim_enabled`` kill-switch (per-overlay effective setting), and
*   the *fail-safe* policy — resolve the local clone whose ``origin`` hosts the
    work item, run the mutex, and on any unreachable-infra outcome do NOT claim /
    do NOT confirm the fence, logging loudly. Turning the switch OFF restores
    today's local-only behaviour.

Keeping this here (not in ``fleet_claim``) preserves the module's Django-free
property so the concurrency proof can run in bare subprocesses.
"""

import contextlib
import logging
import os
from pathlib import Path

from teatree.config import get_effective_settings
from teatree.config.loader import clone_root
from teatree.core import fleet_claim
from teatree.core.worktree.clone_paths import find_clone_path

logger = logging.getLogger(__name__)

_ISSUE_URL_MARKERS = ("/-/issues/", "/-/merge_requests/", "/issues/", "/pull/", "/merge_requests/")


def fleet_claim_enabled(overlay: str) -> bool:
    return get_effective_settings(overlay or None).fleet_claim_enabled


def repo_name_from_issue_url(issue_url: str) -> str:
    """The bare repo name from a forge issue/PR URL (``…/owner/repo/issues/N`` → ``repo``)."""
    path = issue_url.split("://", 1)[-1]
    for marker in _ISSUE_URL_MARKERS:
        if marker in path:
            return path.split(marker, 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return ""


def resolve_claim_repo(issue_url: str) -> str:
    """The local clone to push claim refs from — the one whose ``origin`` hosts the issue.

    Resolved from the issue URL's repo name under the clone root, falling back to
    the teatree main clone (``T3_REPO``). ``""`` when neither resolves, which the
    caller treats as fail-safe (do not claim).
    """
    name = repo_name_from_issue_url(issue_url)
    if name:
        with contextlib.suppress(Exception):
            found = find_clone_path(clone_root(), name)
            if found is not None:
                return str(found)
    repo = os.environ.get("T3_REPO", "").strip()
    return repo if repo and (Path(repo) / ".git").exists() else ""


def acquire_issue_claim(issue_url: str) -> fleet_claim.Claim | None:
    """Win the cross-instance mutex for *issue_url*, or ``None`` (fail-safe).

    The mutex key is the ``issue_url`` alone — the work item is global on the
    shared forge, so two instances must not both implement it regardless of which
    local overlay drives them. ``None`` covers three cases the caller handles
    identically (do not claim): a live rival holds it, no local clone resolved, or
    the ref infra is unreachable (logged loudly). A ref held by a *dead* holder
    (past TTL) is reclaimed via :func:`fleet_claim.steal_if_expired` so a crashed
    instance never wedges the work item forever.
    """
    repo = resolve_claim_repo(issue_url)
    if not repo:
        logger.warning("fleet_claim ON but no local clone resolves for %s — failing safe (not claiming)", issue_url)
        return None
    try:
        claim = fleet_claim.acquire(issue_url, repo=repo)
        if claim is not None:
            return claim
        return fleet_claim.steal_if_expired(issue_url, repo=repo)
    except fleet_claim.FleetClaimUnavailableError:
        logger.warning(
            "fleet_claim ref infra unreachable for %s — failing safe (not claiming)", issue_url, exc_info=True
        )
        return None


def issue_claim_still_held(issue_url: str, sha: str, repo: str) -> bool:
    """THE ship fence: does the claim ref still point at our fencing token ``sha``?

    Fail-safe: an unreachable ref infra returns ``False`` (cannot confirm
    ownership → the caller must refuse the outward write), the same posture as a
    stolen claim. An empty ``sha`` (no claim recorded) is also ``False``.
    """
    if not sha or not repo:
        return False
    try:
        return fleet_claim.is_held_by_me(issue_url, fleet_claim.Claim.from_token(issue_url, sha), repo=repo)
    except fleet_claim.FleetClaimUnavailableError:
        logger.warning(
            "fleet_claim fence: ref infra unreachable for %s — failing safe (treating as not held)",
            issue_url,
            exc_info=True,
        )
        return False
