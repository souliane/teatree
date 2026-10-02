"""Run the operator's own open MRs through the triage ladder and surface each verdict.

The decision is :func:`teatree.core.review.mr_triage.triage`, which is pure; this
module is the other half — it reads the forge and does the arithmetic the ladder
refuses to do, then emits what it found.

It never posts to colleagues or dispatches an action. It does create a durable
owner question when the ladder finds a merge request ready for a review NOBODY
HAS ASKED FOR — a statusline zone is where that fact goes to die, so it is put
to the owner over
:func:`~teatree.core.review.mr_state_question.ask_mr_state`, the bot→owner
channel that carries no publishing gate and is bounded per tick.

The pass is TWO-PASS because a work group is a property of the whole listing: the
groups are built first over everything the operator authored, and only then is each
in-scope merge request triaged. Grouping a scope-filtered subset would make a
cross-repo group look smaller than it is, and a group that looks settled releases a
fragment for review — the premature broadcast
:mod:`teatree.core.gates.review_request_batch_gate` exists to prevent. Reads beyond the
listing — CI enrichment, approval probes, the channel — stay inside the repository
scope, so a sibling outside it is judged from its listing payload alone and holds its
group when that payload cannot show it green.

Facts it cannot read honestly are left UNKNOWN rather than assumed, so an MR whose
review-request state or approval is unreadable surfaces as an owner question instead
of a confident wrong action. With no code host, no scanner is built and none of
this runs.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from django.utils import timezone

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.gates.review_request_guard import LiveAskRead, resolve_guard_target
from teatree.core.models import ReviewRequestPost
from teatree.core.review.mr_ask_state import mr_ask_state, read_asks
from teatree.core.review.mr_ci_state import carries_pipeline_field, ci_state, ci_state_from_status
from teatree.core.review.mr_state_question import ask_mr_state
from teatree.core.review.mr_triage import (
    DEFAULT_THRESHOLDS,
    CiState,
    MrFacts,
    RepoOwner,
    ReviewRequestState,
    TriageAction,
    TriageThresholds,
    TriageVerdict,
    triage,
)
from teatree.core.review.repo_exemption import is_review_exempt, review_exempt_patterns
from teatree.core.review.work_group import GENERIC_SCOPES, group_members
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.my_prs import CiEnricher, _str_field
from teatree.loop.scanners.pr_payload import head_sha
from teatree.loop.scanners.repository_scope import RepositoryScope
from teatree.loop.scanners.review_nag import default_repo_owner
from teatree.types import RawAPIDict
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

#: Verdicts that are the ladder saying "nothing to do here" — a draft, or an MR whose
#: review already happened. Emitting them would be noise, not surveillance.
_QUIET = frozenset({TriageAction.NONE})

_MISSING_REVIEW_OPTIONS = ("Post the review request", "I will ask in person", "It is not ready yet")
_MISSING_REVIEW_REASON = (
    "it is green and out of draft, its work group is ready, and the review channel carries no request for it."
)


def _is_draft(pr: RawAPIDict) -> bool:
    for name in ("draft", "work_in_progress", "isDraft"):
        value = pr.get(name)
        if isinstance(value, bool):
            return value
    return False


def _opened_at(pr: RawAPIDict) -> dt.datetime | None:
    """When the forge says the merge request was opened; ``None`` when it does not say."""
    try:
        return dt.datetime.fromisoformat(_str_field(pr, "created_at", "createdAt"))
    except ValueError:
        return None


@dataclass(slots=True)
class _CiReadings:
    """Every merge request's CI verdict, read at most once per tick.

    A payload carrying no pipeline field costs the enricher a round trip, and a
    grouped merge request is asked about twice — once for its group's readiness,
    once for its own facts — so the reading is memoised rather than repeated.
    """

    read: Callable[[RawAPIDict, str], CiState]
    _by_url: dict[str, CiState] = field(default_factory=dict, init=False)

    def of(self, pr: RawAPIDict, url: str) -> CiState:
        if url not in self._by_url:
            self._by_url[url] = self.read(pr, url)
        return self._by_url[url]


@dataclass(frozen=True, slots=True)
class _WorkGroups:
    """Which of the operator's open merge requests are ONE unit of work, and which are not ready.

    ``unready`` is resolved only for merge requests that HAVE siblings: a group
    of one satisfies the hold trivially, so reading its readiness would buy an
    enrichment round trip and answer nothing.
    """

    members_by_url: dict[str, frozenset[str]]
    unready: frozenset[str]

    def key_for(self, url: str) -> str:
        """The group's stable key, or ``""`` for a merge request that stands alone."""
        members = self.members_by_url.get(url, frozenset())
        return min(members) if len(members) > 1 else ""

    def unready_siblings(self, url: str) -> int:
        return len((self.members_by_url.get(url, frozenset()) - {url}) & self.unready)


@dataclass(frozen=True, slots=True)
class _Survey:
    """One tick's whole read of the world, resolved once before any merge request is triaged.

    Every fact set is built from this, so two merge requests in the same tick can
    never disagree about the listing, the groups, the ledger or the clock.
    """

    merge_requests: dict[str, RawAPIDict]
    requests: dict[str, ReviewRequestPost]
    groups: _WorkGroups
    ci: _CiReadings
    right_now: dt.datetime
    asks: LiveAskRead | None
    exempt_patterns: tuple[str, ...]


@dataclass(slots=True)
class MrTriageScanner:
    """Walk the operator's open MRs, decide each one, and say so.

    ``repo_owner`` and ``thresholds`` are the nag-patience inputs, resolved by the
    wiring layer exactly as the review nag resolves them, so the two never disagree
    about how long a repo waits. ``ci_enricher`` supplies CI for an MR whose list
    payload carries none — the cross-project shape — and is optional: without it
    such an MR simply stays UNKNOWN, which the ladder handles.
    """

    host: CodeHostBackend
    overlay_name: str = ""
    identities: tuple[str, ...] = field(default_factory=tuple)
    allowed_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    repo_owner: Callable[[str], RepoOwner] = default_repo_owner
    thresholds: TriageThresholds = DEFAULT_THRESHOLDS
    ci_enricher: CiEnricher | None = None
    #: Bounds ONE survey pass, so a wide open-MR listing stays one reviewable batch.
    max_mrs_per_tick: int = 20
    now: dt.datetime | None = None
    name: str = "mr_triage"
    _scope: RepositoryScope = field(init=False)

    def __post_init__(self) -> None:
        self._scope = RepositoryScope(self.allowed_url_prefixes, scanner=self.name, log=logger)

    def scan(self) -> list[ScanSignal]:
        if self._scope.refuses():
            return []
        authors = self._resolve_identities()
        if not authors:
            return []
        survey = self._survey(authors)
        signals: list[ScanSignal] = []
        for url, pr in survey.merge_requests.items():
            verdict = triage(self._facts(pr, url=url, survey=survey), thresholds=self.thresholds)
            if verdict.action in _QUIET:
                continue
            if verdict.action is TriageAction.REQUEST_REVIEW:
                ask_mr_state(mr_url=url, reason=_MISSING_REVIEW_REASON, options=_MISSING_REVIEW_OPTIONS)
            signals.append(self._signal(verdict, url=url, title=_str_field(pr, "title")))
            if len(signals) >= self.max_mrs_per_tick:
                break
        return signals

    def _survey(self, authors: tuple[str, ...]) -> _Survey:
        """Pass one: read the listing whole, group it, and resolve each group's readiness."""
        listing = self._collect(authors)
        merge_requests = {url: pr for url, pr in listing.items() if self._scope.admits(url)}
        ci = _CiReadings(read=self._read_ci)
        return _Survey(
            merge_requests=merge_requests,
            requests=self._open_review_requests(),
            groups=self._work_groups(listing, merge_requests, ci),
            ci=ci,
            right_now=self.now or timezone.now(),
            asks=self._asks(merge_requests),
            exempt_patterns=review_exempt_patterns(self.overlay_name),
        )

    def _asks(self, merge_requests: dict[str, RawAPIDict]) -> LiveAskRead | None:
        """One review-channel read for the whole listing; ``None`` leaves every verdict UNKNOWN.

        The channel walk is paginated and rate-limited, so asking it per merge
        request would spend a tick's whole Slack budget on a listing of twenty.
        """
        try:
            return read_asks(merge_requests, target=resolve_guard_target(overlay_name=self.overlay_name))
        except Exception as exc:  # noqa: BLE001 — a channel read must never crash a tick.
            logger.warning("mr_triage: review-channel read failed: %s", exc)
            return None

    def _work_groups(
        self, listing: dict[str, RawAPIDict], in_scope: dict[str, RawAPIDict], ci: _CiReadings
    ) -> _WorkGroups:
        members_by_url = group_members(
            ((url, _str_field(pr, "title")) for url, pr in listing.items()),
            generic_scopes=GENERIC_SCOPES,
        )
        with_siblings = (url for url, members in members_by_url.items() if len(members) > 1)
        return _WorkGroups(
            members_by_url=members_by_url,
            unready=frozenset(
                url for url in with_siblings if not self._review_ready(listing[url], url, ci, in_scope=url in in_scope)
            ),
        )

    @staticmethod
    def _review_ready(pr: RawAPIDict, url: str, ci: _CiReadings, *, in_scope: bool) -> bool:
        """Whether a group member is far enough along to be reviewed with its siblings.

        Draft state and CI both fail CLOSED: a merge request whose pipeline cannot be
        read is not a green, so it holds its group rather than releasing a fragment for
        review. A member outside the repository scope is never enriched, so only a
        pipeline its listing payload already carries can show it green.
        """
        if _is_draft(pr):
            return False
        if in_scope:
            return ci.of(pr, url) is CiState.GREEN
        return carries_pipeline_field(pr) and ci_state(pr) is CiState.GREEN

    @staticmethod
    def _signal(verdict: TriageVerdict, *, url: str, title: str) -> ScanSignal:
        return ScanSignal(
            kind="mr_triage.verdict",
            summary=f"{url} needs {verdict.action.value} ({verdict.reason.value}): {title}",
            payload={
                "url": url,
                "title": title,
                "action": verdict.action,
                "reason": verdict.reason,
                "detail": verdict.detail,
            },
        )

    def _facts(self, pr: RawAPIDict, *, url: str, survey: _Survey) -> MrFacts:
        request = survey.requests.get(url)
        approved = self._approved(url) if request is not None else None
        readable = request is not None and approved is not None
        return MrFacts(
            url=url,
            repo_owner=self.repo_owner(self._slug(url)),
            draft=_is_draft(pr),
            review_exempt=is_review_exempt(self._slug(url), patterns=survey.exempt_patterns),
            ci=survey.ci.of(pr, url),
            work_group=survey.groups.key_for(url),
            work_group_unready_members=survey.groups.unready_siblings(url),
            review_request=self._ask_state(pr, url=url, survey=survey, readable=readable),
            approved=bool(approved) if readable else False,
            idle_since_review_requested=(
                survey.right_now - self._requested_at(request) if readable and request is not None else dt.timedelta()
            ),
        )

    @staticmethod
    def _ask_state(pr: RawAPIDict, *, url: str, survey: _Survey, readable: bool) -> ReviewRequestState:
        """Whether anyone has been asked — the ledger answers first, the channel earns the rest.

        Absence of a ledger row is NOT "nobody was asked": a review requested
        before the ledger existed, or in a repo whose channel nothing watches,
        leaves no row either. So NONE comes from the channel read, which claims
        it only over a window reaching back past this merge request's opening.
        """
        if url in survey.requests:
            return ReviewRequestState.REQUESTED if readable else ReviewRequestState.UNKNOWN
        return mr_ask_state(url, opened_at=_opened_at(pr), read=survey.asks)

    @staticmethod
    def _requested_at(request: ReviewRequestPost) -> dt.datetime:
        """When the reviewers were last spoken to — the re-ping resets the clock."""
        return request.last_nag_at or request.created_at

    def _read_ci(self, pr: RawAPIDict, url: str) -> CiState:
        if carries_pipeline_field(pr) or self.ci_enricher is None:
            return ci_state(pr)
        return ci_state_from_status(self.ci_enricher.status_for(url=url, head_sha=head_sha(pr)))

    def _approved(self, url: str) -> bool | None:
        """Whether anyone has approved, or ``None`` when the probe could not answer.

        Read only for MRs a review was requested on, so the cost is bounded by the
        ledger rather than by the MR list. ``None`` is not "unapproved": claiming
        that would surface a group ping for a review that may already be done, so an
        unreadable probe takes the MR out of the requested lane entirely and it
        surfaces as an owner question instead.
        """
        ref = pr_ref_from_url(url)
        if ref is None:
            return None
        try:
            state = self.host.get_mr_approvals(repo=ref.slug, pr_iid=ref.pr_id)
        except Exception as exc:  # noqa: BLE001 — an approval probe must never crash a tick.
            logger.warning("mr_triage: approval probe failed for %s: %s", url, exc)
            return None
        # ``approvals_left`` — not ``approved_by`` — is the field both backends populate
        # correctly: GitHub hard-codes ``approved_by=[]`` unconditionally (#8), so reading
        # its truthiness misreads every GitHub-backed approval as unapproved forever.
        return state.get("approvals_left", 1) <= 0

    @staticmethod
    def _slug(url: str) -> str:
        ref = pr_ref_from_url(url)
        return ref.slug if ref is not None else ""

    def _open_review_requests(self) -> dict[str, ReviewRequestPost]:
        rows = ReviewRequestPost.objects.filter(done_at__isnull=True, overlay=self.overlay_name)
        return {row.mr_url: row for row in rows}

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _collect(self, authors: tuple[str, ...]) -> dict[str, RawAPIDict]:
        """Every open merge request the operator authored, keyed by URL — the whole listing, which grouping needs."""
        collected: dict[str, RawAPIDict] = {}
        for author in authors:
            try:
                fetched = self.host.list_my_prs(author=author)
            except Exception:
                logger.warning("mr_triage: list_my_prs failed for %s — skipping", author, exc_info=True)
                continue
            for pr in fetched:
                if url := _str_field(pr, "web_url", "html_url"):
                    collected.setdefault(url, pr)
        return collected


__all__ = ["MrTriageScanner"]
