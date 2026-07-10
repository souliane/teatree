"""Django-aware wiring for the :mod:`teatree.core.fleet.claim` mutex.

:mod:`teatree.core.fleet.claim` is deliberately Django-free (a repo path in, a
git CAS out). This thin layer supplies the two things the claim + ship-fence call
sites need from the Django world and nowhere else:

*   the ``fleet_claim_enabled`` kill-switch (per-overlay effective setting), and
*   the *fail-safe* policy — resolve the local clone whose ``origin`` hosts the
    work item, run the mutex, and on any unreachable-infra outcome do NOT claim /
    do NOT confirm the fence, logging loudly. Turning the switch OFF restores
    today's local-only behaviour.

Keeping this here (not in ``claim``) preserves that module's Django-free
property so the concurrency proof can run in bare subprocesses.
"""

import contextlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from teatree.config import get_effective_settings
from teatree.config.loader import clone_root
from teatree.core.fleet import claim
from teatree.core.worktree.clone_paths import find_clone_path
from teatree.instance_id import instance_id
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.core.models import ImplementedIssueMarker, Ticket

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


def owner_repo_from_issue_url(issue_url: str) -> str:
    """The ``owner/repo`` (or ``group/sub/repo``) slug from a forge issue/PR URL."""
    path = issue_url.split("://", 1)[-1]
    for marker in _ISSUE_URL_MARKERS:
        if marker in path:
            base = path.split(marker, 1)[0].rstrip("/")
            host_and_slug = base.split("/", 1)
            return host_and_slug[1] if len(host_and_slug) > 1 else ""
    return ""


def _resolve_clone(issue_url: str) -> str:
    name = repo_name_from_issue_url(issue_url)
    if name:
        with contextlib.suppress(Exception):
            found = find_clone_path(clone_root(), name)
            if found is not None:
                return str(found)
    repo = os.environ.get("T3_REPO", "").strip()
    return repo if repo and (Path(repo) / ".git").exists() else ""


def _origin_hosts_issue(repo: str, expected_slug: str) -> bool:
    """Whether *repo*'s ``origin`` remote slug equals *expected_slug* (case-insensitive)."""
    with contextlib.suppress(Exception):
        return git.remote_slug(repo=repo).casefold() == expected_slug.casefold()
    return False


def resolve_claim_repo(issue_url: str) -> str:
    """The local clone to push claim refs from — one whose ``origin`` HOSTS the issue.

    A NAME-only match (or the ``T3_REPO`` fallback) can resolve a clone whose
    ``origin`` points at a DIFFERENT forge repo than the issue lives on; pushing the
    claim there would split the mutex across remotes and let two instances both
    claim the same issue. So the resolved clone's ``origin`` slug MUST match the
    issue URL's ``owner/repo`` — on a mismatch, an unparsable slug, or an absent
    origin, return ``""`` and fail SAFE (do not claim) rather than push to the wrong
    remote.
    """
    candidate = _resolve_clone(issue_url)
    if not candidate:
        return ""
    expected = owner_repo_from_issue_url(issue_url)
    if not expected or not _origin_hosts_issue(candidate, expected):
        logger.warning(
            "fleet_claim: resolved clone %s does not host %s (origin mismatch) — failing safe (not claiming)",
            candidate,
            expected or issue_url,
        )
        return ""
    return candidate


def acquire_issue_claim(issue_url: str) -> claim.Claim | None:
    """Win the cross-instance mutex for *issue_url*, or ``None`` (fail-safe).

    The mutex key is the ``issue_url`` alone — the work item is global on the
    shared forge, so two instances must not both implement it regardless of which
    local overlay drives them. ``None`` covers three cases the caller handles
    identically (do not claim): a live rival holds it, no local clone resolved, or
    the ref infra is unreachable (logged loudly). A ref held by a *dead* holder
    (past TTL) is reclaimed via :func:`claim.steal_if_expired` so a crashed
    instance never wedges the work item forever.
    """
    repo = resolve_claim_repo(issue_url)
    if not repo:
        logger.warning("fleet_claim ON but no local clone resolves for %s — failing safe (not claiming)", issue_url)
        return None
    try:
        acquired = claim.acquire(issue_url, repo=repo)
        if acquired is not None:
            return acquired
        return claim.steal_if_expired(issue_url, repo=repo)
    except claim.FleetClaimUnavailableError:
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
        return claim.is_held_by_me(issue_url, claim.Claim.from_token(issue_url, sha), repo=repo)
    except claim.FleetClaimUnavailableError:
        logger.warning(
            "fleet_claim fence: ref infra unreachable for %s — failing safe (treating as not held)",
            issue_url,
            exc_info=True,
        )
        return False


def ticket_claim_is_lost(ticket: "Ticket", repo: str) -> bool:
    """The outward-write fence shared by the ship gate, ``execute_ship`` and ``ensure-pr``.

    ``True`` (= ABORT the write) iff the kill-switch is ON for the ticket's overlay,
    the ticket carries a fleet claim (an issue-implementer marker with a fencing
    sha), and the claim ref no longer confirms this instance holds it — stolen, or
    the ref infra is unreachable so ownership cannot be confirmed (fail CLOSED).
    ``False`` when the switch is off, no claim exists, or we still hold it — so a
    non-fleet ship is never affected.

    The marker is resolved by its own natural key ``(issue_url, overlay)`` — the key
    ``ImplementedIssueMarkerManager.cache_from_fleet_claim`` populates on the dispatch
    path. The marker's ``ticket`` FK is never set in production, so keying the fence
    on ``ticket`` would find nothing and leave the fence a permanent no-op.
    """
    if not fleet_claim_enabled(ticket.overlay):
        return False
    if not ticket.issue_url:
        return False
    from teatree.core.models import ImplementedIssueMarker  # noqa: PLC0415 — leaf import kept out of module load

    marker = (
        ImplementedIssueMarker.objects.filter(issue_url=ticket.issue_url, overlay=ticket.overlay)
        .exclude(claim_ref_sha="")
        .first()
    )
    if marker is None:
        return False
    return not issue_claim_still_held(marker.issue_url, marker.claim_ref_sha, repo)


def heartbeat_inflight_claims(overlay: str) -> None:
    """Keep every in-flight fleet claim for *overlay* un-stealable (Stage 2, B1).

    The heartbeat sweep. Runs on the issue-implementer tick — a cadence comfortably
    shorter than the TTL — so a live claim is re-affirmed long before it could
    expire and be stolen while its dispatch runs. Per non-abandoned marker carrying
    a fencing sha: re-point the claim ref (CAS against our own sha) and PERSIST the
    refreshed sha. A ``ClaimLost`` means another instance stole it while our dispatch
    stalled — abandon the marker so the in-flight work aborts rather than push under
    a lost claim. An unreachable forge is transient: leave the marker and retry next
    tick (the TTL is the backstop).
    """
    if not fleet_claim_enabled(overlay):
        return
    from teatree.core.models import ImplementedIssueMarker  # noqa: PLC0415 — leaf import kept out of module load

    live = (
        ImplementedIssueMarker.objects.filter(overlay=overlay)
        .exclude(claim_ref_sha="")
        .exclude(state=ImplementedIssueMarker.State.ABANDONED)
    )
    for marker in live:
        _heartbeat_one(marker)


def _heartbeat_one(marker: "ImplementedIssueMarker") -> None:
    from teatree.core.models import ImplementedIssueMarker  # noqa: PLC0415 — leaf import kept out of module load

    repo = resolve_claim_repo(marker.issue_url)
    if not repo:
        return
    held = claim.Claim(
        work_key=marker.issue_url,
        ref=claim.claim_ref(marker.issue_url),
        sha=marker.claim_ref_sha,
        instance_id=instance_id(),
        claimed_at=0.0,
        ttl_seconds=claim.DEFAULT_TTL_SECONDS,
    )
    try:
        result = claim.heartbeat(held, repo=repo)
    except claim.FleetClaimUnavailableError:
        logger.warning(
            "fleet_claim heartbeat: ref infra unreachable for %s — leaving claim, retrying next tick",
            marker.issue_url,
            exc_info=True,
        )
        return
    if isinstance(result, claim.ClaimLost):
        logger.warning(
            "fleet_claim for %s was STOLEN (ref now %s) — abandoning in-flight work",
            marker.issue_url,
            result.observed_sha or "<deleted>",
        )
        marker.state = ImplementedIssueMarker.State.ABANDONED
        marker.save(update_fields=["state"])
        return
    marker.claim_ref_sha = result.sha
    marker.save(update_fields=["claim_ref_sha"])
