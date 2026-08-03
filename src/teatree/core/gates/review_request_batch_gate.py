"""R1 — no merge request is broadcast for review while a sibling in its work group is not ready.

A unit of work that arrived as three merge requests is reviewed as three merge
requests or not at all. Releasing the first one the moment it goes green hands a
reviewer a third of a change and asks them to guess at the rest, then interrupts
them twice more as the siblings land. So a member waits until EVERY open member
of its group is review-ready, and then they all go at once.

The group comes from :func:`~teatree.core.review.work_group.group_members` over
the operator's GLOBAL open-merge-request listing — every accessible project, not
the subject's own repo. A per-repo or url-prefixed listing makes a cross-repo
group look smaller than it is, and a group that looks complete is released
early: precisely the premature broadcast this gate exists to prevent.

**Every axis fails CLOSED.** An unreadable draft state, a pipeline status the
allowlist does not name, an unreadable pause, an unresolvable host and a merge
request absent from the listing all answer NOT ready. This is not caution for
its own sake — the sibling gate
:mod:`~teatree.core.gates.review_request_draft_gate` shipped the opposite shape
once, and a probe that answered the benign value whenever it could not read the
forge made the owner's hold mechanism inert at exactly the moment it was needed,
silently. A gate whose failure mode is "broadcast anyway" is not a gate.

The one axis that does NOT hold silently is an oversize group. Past
``work_group_max_members`` the shared signal is likelier to be a coincidence
than one unit of work, so the merge request is surfaced to the owner as a
question (:func:`~teatree.core.review.mr_state_question.ask_mr_state`) rather
than disappearing into a hold nobody can see.

A review-exempt member is always listed, because the operator still wants to see
it; whether it HOLDS the group is
``review_exempt_repos_count_toward_group_readiness``. The default counts it,
which is the conservative reading — an unready infrastructure change the batch
depends on keeps holding the batch — and it is never broadcast either way.

Ships INERT: the chokepoints consult :func:`work_group_batch_refusal`, a no-op
until ``require_work_group_batch`` is turned on.
"""

import logging
from dataclasses import dataclass

from teatree.config import get_effective_settings
from teatree.core.backend_factory import code_host_from_overlay, messaging_from_overlay
from teatree.core.backend_protocols import DraftState
from teatree.core.gates.review_request_draft_gate import draft_state
from teatree.core.gates.review_request_guard import canonical_mr_url
from teatree.core.models import ReviewRequestPost
from teatree.core.review.mr_ci_state import ci_state
from teatree.core.review.mr_state_question import ask_mr_state
from teatree.core.review.mr_triage import CiState
from teatree.core.review.repo_exemption import mr_url_is_review_exempt
from teatree.core.review.review_pause import PauseState, read_pause_state
from teatree.core.review.work_group import group_members
from teatree.core.review.work_group_settings import generic_scopes_from_settings
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

HOST_UNAVAILABLE = "code_host_unavailable"
HOST_UNREADABLE = "code_host_unreadable"
GROUP_UNRESOLVED = "work_group_unresolved"
GROUP_TOO_LARGE = "work_group_too_large"
MEMBER_NOT_READY = "work_group_member_not_ready"

#: The refusal a chokepoint prints, whichever axis produced it — one string the
#: caller branches on, with the per-member detail carried in ``blockers``.
REFUSAL_REASON = "work_group_not_ready"

_DRAFT_BLOCKERS = {DraftState.DRAFT: "draft", DraftState.UNKNOWN: "draft_state_unknown"}
_CI_BLOCKERS = {CiState.PENDING: "ci_pending", CiState.FAILED: "ci_failed", CiState.UNKNOWN: "ci_unknown"}
_PAUSE_BLOCKERS = {PauseState.PAUSED: "paused", PauseState.UNKNOWN: "pause_unknown"}

_OVERSIZE_OPTIONS = ("Broadcast this merge request on its own", "Keep holding the whole group")


@dataclass(frozen=True, slots=True)
class MemberReadiness:
    """One group member's verdict — ``blockers`` empty means review-ready."""

    mr_url: str
    blockers: tuple[str, ...] = ()
    review_exempt: bool = False

    @property
    def ready(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class BatchVerdict:
    ready: bool
    reason: str
    group_key: str
    blockers: tuple[str, ...]
    members: tuple[MemberReadiness, ...] = ()


def work_group_ready(*, mr_url: str, overlay_name: str = "") -> BatchVerdict:
    """Whether *mr_url*'s whole work group may be broadcast, and what holds it back.

    Asks the owner when the group is oversize, so an implausibly large group
    surfaces as a question instead of an invisible hold.
    """
    canonical = canonical_mr_url(mr_url)
    listing = _OpenMergeRequests.read(overlay_name)
    if isinstance(listing, str):
        return BatchVerdict(ready=False, reason=listing, group_key=canonical, blockers=())
    verdict = listing.verdict_for(canonical)
    if verdict.reason == GROUP_TOO_LARGE:
        ask_mr_state(mr_url=canonical, reason=_oversize_reason(verdict, overlay_name), options=_OVERSIZE_OPTIONS)
    return verdict


def work_groups(*, overlay_name: str = "") -> tuple[BatchVerdict, ...]:
    """Every work group across the operator's open merge requests — a read-only survey.

    Raises no question and takes no claim, so a status surface can render the
    whole picture without side effects. An unresolvable or unreadable host comes
    back as one refusing verdict rather than an empty survey, so a failed read
    can never be mistaken for "nothing in flight".
    """
    listing = _OpenMergeRequests.read(overlay_name)
    if isinstance(listing, str):
        return (BatchVerdict(ready=False, reason=listing, group_key="", blockers=()),)
    return listing.every_verdict()


def work_group_batch_refusal(mr_url: str, *, overlay_name: str = "") -> BatchVerdict | None:
    """The refusing verdict when the gate is ARMED and the group is not ready; ``None`` otherwise.

    The single call each chokepoint makes, so ``check`` can never predict a
    verdict ``post`` then contradicts.
    """
    if not get_effective_settings(overlay_name or None).require_work_group_batch:
        return None
    verdict = work_group_ready(mr_url=mr_url, overlay_name=overlay_name)
    return None if verdict.ready else verdict


def refusal_payload(verdict: BatchVerdict, *, mr_url: str) -> RawAPIDict:
    """The machine-legible refusal both chokepoints emit — one shape, one place."""
    return {"action": "refused", "reason": REFUSAL_REASON, "mr_url": mr_url, "blockers": list(verdict.blockers)}


def post_command_lines(verdict: BatchVerdict) -> tuple[str, ...]:
    """The ordered ``t3 review-request post`` invocations a ready group is broadcast with.

    A review-exempt member is skipped: the same repo exemption that keeps it from
    holding the batch also refuses its own post, so offering the line would hand
    the operator a command guaranteed to refuse.
    """
    if not verdict.ready:
        return ()
    return tuple(
        f"t3 review-request post --mr-url {member.mr_url} --approver <your-user-id>"
        for member in verdict.members
        if not member.review_exempt
    )


@dataclass(frozen=True, slots=True)
class _OpenMergeRequests:
    """The operator's open merge requests, listed and grouped once."""

    overlay_name: str
    payload_by_url: dict[str, RawAPIDict]
    groups: dict[str, frozenset[str]]

    @classmethod
    def read(cls, overlay_name: str) -> "_OpenMergeRequests | str":
        """The listing, or the refusal reason naming why it could not be read."""
        host = code_host_from_overlay(overlay_name or None)
        if host is None:
            logger.warning("review_request batch gate: no code host resolved (overlay %r)", overlay_name)
            return HOST_UNAVAILABLE
        try:
            listed = host.list_my_prs(author=host.current_user())
        except Exception:
            logger.exception("review_request batch gate: open merge request listing failed — holding every group")
            return HOST_UNREADABLE
        payload_by_url = {url: payload for payload in listed if (url := _canonical_url_of(payload))}
        titles = ((url, str(payload.get("title") or "")) for url, payload in payload_by_url.items())
        return cls(
            overlay_name=overlay_name,
            payload_by_url=payload_by_url,
            groups=group_members(titles, generic_scopes=generic_scopes_from_settings(overlay_name)),
        )

    def verdict_for(self, mr_url: str) -> BatchVerdict:
        group = self.groups.get(mr_url)
        if group is None:
            logger.warning("review_request batch gate: %s is absent from the open listing — holding", mr_url)
            return BatchVerdict(ready=False, reason=GROUP_UNRESOLVED, group_key=mr_url, blockers=())
        return self._verdict(group)

    def every_verdict(self) -> tuple[BatchVerdict, ...]:
        keys = {min(group) for group in self.groups.values()}
        return tuple(sorted((self._verdict(self.groups[key]) for key in keys), key=lambda verdict: verdict.group_key))

    def _verdict(self, group: frozenset[str]) -> BatchVerdict:
        settings = get_effective_settings(self.overlay_name or None)
        group_key = min(group)
        if len(group) > settings.work_group_max_members:
            return BatchVerdict(ready=False, reason=GROUP_TOO_LARGE, group_key=group_key, blockers=())
        members = tuple(self._member_readiness(url) for url in sorted(group))
        gating = (
            members
            if settings.review_exempt_repos_count_toward_group_readiness
            else tuple(member for member in members if not member.review_exempt)
        )
        blockers = tuple(blocker for member in gating for blocker in member.blockers)
        return BatchVerdict(
            ready=not blockers,
            reason=MEMBER_NOT_READY if blockers else "",
            group_key=group_key,
            blockers=blockers,
            members=members,
        )

    def _member_readiness(self, mr_url: str) -> MemberReadiness:
        blockers = (
            _DRAFT_BLOCKERS.get(draft_state(mr_url, overlay_name=self.overlay_name)),
            _CI_BLOCKERS.get(ci_state(self.payload_by_url[mr_url])),
            self._pause_blocker(mr_url),
        )
        return MemberReadiness(
            mr_url=mr_url,
            blockers=tuple(f"{mr_url}: {code}" for code in blockers if code),
            review_exempt=mr_url_is_review_exempt(mr_url, overlay_name=self.overlay_name),
        )

    def _pause_blocker(self, mr_url: str) -> str:
        """The pause-axis code, or ``""``.

        A merge request nobody has broadcast yet carries no hold to read, and one
        the owner has explicitly resumed carries a hold they already lifted — so
        neither costs a messaging round trip, and a transport failure at that
        point cannot re-arm a pause that is provably over.
        """
        post = ReviewRequestPost.objects.filter(mr_url=mr_url).first()
        if post is None or post.resumed_at is not None:
            return ""
        return _PAUSE_BLOCKERS.get(read_pause_state(post, messaging_from_overlay(self.overlay_name or None)), "")


def _canonical_url_of(payload: RawAPIDict) -> str:
    for key in ("web_url", "html_url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return canonical_mr_url(value)
    return ""


def _oversize_reason(verdict: BatchVerdict, overlay_name: str) -> str:
    cap = get_effective_settings(overlay_name or None).work_group_max_members
    return (
        f"its work group holds more than {cap} open merge requests (work_group_max_members), "
        f"so the shared signal is likelier to be a coincidence than one unit of work — "
        f"group key {verdict.group_key}."
    )
