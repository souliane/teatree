"""The ONE issue-intake scanner — unified candidate discovery behind one decision (#3634).

Folds the two former intake scanners (``assigned_issues`` and
``issue_implementer``) into a single loop job so the factory can never hold two
divergent opinions about which issue becomes work. Discovery is a union of two
author-/label-scoped forge queries; the verdict is
:func:`~teatree.core.intake.factory_admission.decide_issue_intake`, evaluated top-down.

Discovery is scoped so the factory never even fetches work it may not do:

* one author-scoped query per handle in the trusted union, bound to the
    overlay's own repo slugs — a stranger's issue is never fetched;
* one label-scoped query for the owner-applied admit label, same repo scope —
    this is the ONLY route by which an untrusted author's issue enters, and it
    requires the owner's explicit label (the admit-label rule).

Selection narrows; the decision function decides. Every candidate is re-checked
at claim time through the shared :mod:`~teatree.core.review.author_trust` seam,
so an over-returning forge query cannot launder an untrusted author past the
fail-closed last rule.

An UMBRELLA/epic row is declined outright (#4105). Discovery cannot exclude it —
it is authored by the same trusted human as everything else — so the decision
table refuses it: an epic's scope is unbounded, so it holds a bounded in-flight
slot with no state of the world that ends the claim, displacing implementable
work for the whole run.

Claims go through the TOCTOU-safe :meth:`ImplementedIssueMarker.claim` (or the
cross-instance fleet ref when that kill-switch is on), so a re-tick or a
concurrent overlay never double-dispatches.

Candidates are claimed OLDEST FILED FIRST (#4238). Each discovery query asks its forge
for that order, and the merged set is re-sorted here — sorting inside one query says
nothing about the union of several, so the merge is where fairness is actually decided.

Age order alone is not starvation-free, because a decided candidate never leaves the scan
set: an issue that already has work is re-fetched and re-judged every tick, so the prefix of
already-decided issues grows without bound. Under a fixed scan budget the walk was abandoned
inside that prefix and the frontier — where the only still-claimable issues live — was never
reached, so nothing filed was admitted (#4466). Two things keep the frontier reachable: the
per-tick :class:`~teatree.loop.scanners.forge_readback.ReadbackIndex`, which makes re-deciding
a decided issue a bucket lookup rather than a scan of every PR, and a resume CURSOR, so a pass
that still runs out of budget continues at the frontier next tick and wraps to the oldest
after it — never restarting from the oldest and dropping the same tail forever.

Every admissible candidate the budget or the governor stopped us from claiming is
recorded in :class:`UnclaimedIntakeCandidate`, because intake's own decision is per-tick
and log-only: without the ledger a passed-over issue is indistinguishable from one that
was never filed, which is how three issues went unadmitted for a full day with every
health surface green.
"""

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from django.apps import apps

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.fleet import wire
from teatree.core.intake.factory_admission import (
    IntakeLabelPolicy,
    IntakeVerdict,
    decide_issue_intake,
    payload_body,
    payload_labels,
)
from teatree.core.intake.umbrella import umbrella_reason
from teatree.core.models import ImplementedIssueMarker, IntakeScanCursor, UnclaimedIntakeCandidate, WaitingCandidate
from teatree.core.review.author_trust import (
    AuthorSubject,
    AutonomyGate,
    TrustVerdict,
    decide_author_trust,
    trusted_handles,
)
from teatree.core.work_lease import WorkIdentity, foreign_work_holder
from teatree.instance_id import instance_id
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.forge_readback import (
    ReadbackIndex,
    build_readback_index,
    fetch_merged_prs,
    fetch_open_prs,
    issue_number,
)
from teatree.types import RawAPIDict
from teatree.utils.url_slug import slug_from_issue_or_pr_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

#: Where an issue with no readable filing date sorts. LAST, so a payload-shape change
#: degrades the whole queue to arrival order instead of letting one undated issue
#: overtake every dated one waiting ahead of it.
_UNDATED = dt.datetime.max.replace(tzinfo=dt.UTC)


@dataclass(frozen=True, slots=True)
class _TickContext:
    """The per-tick facts every candidate is decided against."""

    tracked: frozenset[str]
    trusted: frozenset[str]
    readback: ReadbackIndex


def issue_url(issue: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_title(issue: RawAPIDict) -> str:
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def issue_created_at(issue: RawAPIDict) -> dt.datetime | None:
    """When *issue* was FILED — the intake queue's ordering key on both forges.

    GitHub and GitLab both name the field ``created_at``. A naive timestamp is read as
    UTC, which is what both forges emit; an absent or unparsable one yields ``None``.
    """
    raw = issue.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        moment = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def issue_author(issue: RawAPIDict) -> str:
    """The handle that FILED *issue*, across both forges' payload shapes.

    GitHub nests the author under ``user.login``; GitLab under ``author.username``.
    Returns ``""`` when no author can be resolved — which the gate reads as UNTRUSTED,
    never as a wildcard.
    """
    for container in ("user", "author"):
        value = issue.get(container)
        if not isinstance(value, dict):
            continue
        for name in ("login", "username"):
            handle = cast("RawAPIDict", value).get(name)
            if isinstance(handle, str) and handle.strip():
                return handle.strip()
    return ""


def _issue_is_open(issue: RawAPIDict) -> bool:
    """Treat an issue as open unless the backend explicitly reports otherwise."""
    state = issue.get("state")
    return not (isinstance(state, str) and state.lower() == "closed")


def _issue_slug_and_host_kind(url: str) -> tuple[str, str]:
    """The ``(repo_slug, host_kind)`` the author classifier needs, from an issue URL.

    An unrecognised URL yields an EMPTY slug, which the gate refuses — an
    unclassifiable issue is never claimable.
    """
    parsed = urlparse(url)
    slug = slug_from_issue_or_pr_url(parsed.path)
    is_gitlab = "/-/" in parsed.path or "gitlab" in (parsed.hostname or "").lower()
    return slug, "gitlab" if is_gitlab else "github"


def author_is_trusted(issue: RawAPIDict, trusted: frozenset[str]) -> bool:
    """The fail-closed per-issue author gate — REFUSE unless the filer is a named trusted human.

    Delegates to :func:`decide_author_trust` at the ``INTAKE`` gate — the ONE autonomy
    decision the PR-merge gate also applies (#3577) — so the factory cannot hold two
    opinions of who is trusted. The intake gate's extra strictness (EXPLICIT trusted-set
    membership on top of the repo-scoped classification, closing the private-repo bypass)
    lives in that decision, not here. An unresolvable author or repo slug refuses before
    the decision is reached.
    """
    author = issue_author(issue)
    slug, host_kind = _issue_slug_and_host_kind(issue_url(issue))
    if not author or not slug:
        return False
    subject = AuthorSubject(slug=slug, author=author, host_kind=host_kind)
    return decide_author_trust(subject, gate=AutonomyGate.INTAKE, extra_trusted=trusted) is TrustVerdict.AUTONOMOUS


@dataclass(slots=True)
class IssueIntakeScanner:
    """Discover and claim admissible open issues for the factory (#3634).

    ``admit_label`` is the owner-applied admission label (the effective
    ``issue_implementer_label``). It is BOTH the label-scoped discovery query and
    the admit-label rule of the decision table — an untrusted author's issue enters
    only through it.

    ``trusted_authors`` is the CONFIG tier of the trust union (the owner's
    ``user_identity_aliases`` plus the ``trusted_issue_authors`` allowlist); the DB
    tier (``TrustedIdentity`` rows) is unioned in here. An EMPTY union trusts
    NOBODY — the label-scoped query still runs, so an owner-labelled issue is still
    admissible.

    ``identities`` is the OPERATOR's own handle set. It is deliberately NOT the
    trust set: it scopes the read-back's PR queries, because the PR implementing an
    issue is authored by the operator regardless of who filed the issue.

    ``exclude_labels`` is the overlay's denylist (``OverlayConfig.exclude_labels``) —
    the exclude rule of the decision table. It holds an issue whoever filed it, so it is
    the operator's reservation surface against the factory (#4134).
    """

    host: CodeHostBackend
    admit_label: str
    overlay_name: str = ""
    #: Labels marking an umbrella/epic parent — the umbrella rule's operator-maintained half,
    #: resolved from ``umbrella_issue_labels``. Empty is the honest "none configured",
    #: not a stand-in for the shipped set: the structural half needs no configuration,
    #: so an empty set still declines an unlabelled epic.
    umbrella_labels: frozenset[str] = frozenset()
    trusted_authors: tuple[str, ...] = field(default_factory=tuple)
    identities: tuple[str, ...] = field(default_factory=tuple)
    exclude_labels: tuple[str, ...] = field(default_factory=tuple)
    #: The overlay's OWN repo slugs (``owner/name``). Every discovery query is
    #: scoped to them. Empty keeps the pre-scope global search (back-compat).
    repo_slugs: tuple[str, ...] = field(default_factory=tuple)
    name: str = "issue_intake"
    readback_enabled: bool = True
    #: The single-ticket in-flight budget; 0 means uncapped.
    max_concurrent: int = 0
    #: When False this tick claims nothing new. It still HEARTBEATS in-flight fleet
    #: claims (one would otherwise expire mid-dispatch) and still runs discovery, so
    #: the queue an unclaimable tick is sitting on is recorded rather than unseen —
    #: the full-budget tick is exactly when a starved issue needs a witness (#4238).
    can_claim: bool = True
    #: The scanner's OWN deadline for the candidate walk. Deliberately below the scan
    #: phase's pool deadline: past that one the thread is abandoned rather than stopped,
    #: so it keeps mutating rows after the tick ended and records no resume point.
    pass_budget_seconds: float = 45.0
    #: Injected for tests — the real clock is what bounds the walk in production.
    monotonic: "Callable[[], float]" = time.monotonic

    def scan(self) -> list[ScanSignal]:
        wire.heartbeat_inflight_claims(self.overlay_name)
        trusted = self._trusted_author_set()
        candidates = self._candidate_issues(trusted)
        if not candidates:
            UnclaimedIntakeCandidate.objects.sync(self.overlay_name, [])
            return []
        operators = self._resolve_identities()
        context = _TickContext(
            tracked=self._tracked_issue_urls(),
            trusted=trusted,
            readback=self._readback_index(operators),
        )
        signals: list[ScanSignal] = []
        waiting: list[WaitingCandidate] = []
        claiming = self.can_claim
        walk = self._resume_ordered(candidates)
        deadline = self.monotonic() + self.pass_budget_seconds
        examined: RawAPIDict | None = None
        for position, issue in enumerate(walk):
            if self.monotonic() >= deadline:
                self._report_incomplete(walk, position)
                break
            examined = issue
            url = issue_url(issue)
            try:
                verdict = self._admits(issue, url, context=context)
                if verdict is None:
                    continue
                claiming = claiming and not (self._budget_exhausted() or self._governor_denied())
                if claiming:
                    self._append_claim(issue, url, verdict, signals)
                    continue
            except Exception:
                logger.exception("IssueIntakeScanner failed on issue %s", url)
                continue
            waiting.append(
                WaitingCandidate(issue_url=url, title=_issue_title(issue), issue_created_at=issue_created_at(issue)),
            )
        else:
            position = len(walk)
        complete = position >= len(walk)
        UnclaimedIntakeCandidate.objects.sync(self.overlay_name, waiting, complete=complete)
        self._record_pass(examined, complete=complete)
        return signals

    def _readback_index(self, operators: tuple[str, ...]) -> ReadbackIndex:
        """The tick's PR corpus, bucketed once so a candidate reads only what could cite it."""
        if not self.readback_enabled:
            return build_readback_index([], [])
        return build_readback_index(
            fetch_open_prs(self.host, authors=operators),
            fetch_merged_prs(self.host, authors=operators),
        )

    def _resume_ordered(self, candidates: list[RawAPIDict]) -> list[RawAPIDict]:
        """*candidates* rotated to start after the last pass's stopping point.

        Age order is preserved WITHIN the rotation, and the wrap is what guarantees the
        oldest candidates are reached again once the frontier has been: a walk that only
        ever moved forward would starve the head of the queue instead of its tail.
        """
        resume_after = IntakeScanCursor.objects.resume_after(self.overlay_name)
        if not resume_after:
            return candidates
        urls = [issue_url(issue) for issue in candidates]
        if resume_after not in urls:
            return candidates
        start = urls.index(resume_after) + 1
        return candidates[start:] + candidates[:start]

    @staticmethod
    def _report_incomplete(walk: list[RawAPIDict], position: int) -> None:
        unreached = walk[position:]
        logger.warning(
            "IssueIntakeScanner ran out of budget after %d/%d candidates; %d unreached, oldest %s",
            position,
            len(walk),
            len(unreached),
            issue_url(unreached[0]) if unreached else "",
        )

    def _record_pass(self, examined: RawAPIDict | None, *, complete: bool) -> None:
        if examined is None:
            return
        IntakeScanCursor.objects.record_pass(
            self.overlay_name,
            last_issue_url=issue_url(examined),
            last_issue_created_at=issue_created_at(examined),
            complete=complete,
        )

    def _label_policy(self) -> IntakeLabelPolicy:
        """The overlay's two configured label sets, as the ONE value the table reads."""
        return IntakeLabelPolicy(exclude=frozenset(self.exclude_labels), umbrella=self.umbrella_labels)

    def _admits(self, issue: RawAPIDict, url: str, *, context: "_TickContext") -> "IntakeVerdict | None":
        """The admitting verdict for *issue*, or ``None`` when the table refuses it.

        Decides only — the claim is a separate step, so a candidate can be judged
        admissible on a tick that has no budget to act on it.

        The "work exists" fact is the union of the local ticket ledger and the forge
        read-back, so a cross-instance PR that already cites the issue is seen even
        though no local row exists.
        """
        work_exists = bool(url) and url in context.tracked
        detail = ""
        if not work_exists and self.readback_enabled:
            hit = context.readback.hit_for(issue_url=url, ticket_number=issue_number(url))
            if hit is not None:
                work_exists = True
                detail = f"{hit.reason} ({hit.evidence_url})"
        verdict = decide_issue_intake(
            issue,
            author_trusted=author_is_trusted(issue, context.trusted),
            work_exists=work_exists,
            admit_label=self.admit_label,
            label_policy=self._label_policy(),
        )
        if verdict.acts:
            return verdict
        if verdict is IntakeVerdict.IGNORE_UMBRELLA:
            # Re-derived rather than threaded out of the verdict: an enum member cannot
            # carry a per-issue reason, and a decline with no account of itself is how an
            # issue disappears from intake with every surface still reading green.
            detail = umbrella_reason(
                body=payload_body(issue),
                labels=payload_labels(issue),
                umbrella_labels=self.umbrella_labels,
            )
        logger.info(
            "IssueIntakeScanner %s %s (author %r)%s",
            verdict.value,
            url,
            issue_author(issue),
            f": {detail}" if detail else "",
        )
        return None

    def _append_claim(
        self,
        issue: RawAPIDict,
        url: str,
        verdict: "IntakeVerdict",
        signals: list[ScanSignal],
    ) -> None:
        """Claim *issue* and append its signal; a refused claim appends nothing.

        A refusal means an existing marker or a live work lease already holds the issue,
        so it is NOT a waiting candidate — someone is on it. That distinction is why the
        claim attempt is the last step: only a candidate we never tried to claim is one
        the budget passed over.
        """
        if self._claim(url) is None:
            return
        signals.append(self._signal(issue, url, verdict))

    def _signal(self, issue: RawAPIDict, url: str, verdict: "IntakeVerdict") -> ScanSignal:
        return ScanSignal(
            kind="issue_intake.admitted",
            summary=f"Admitted for auto-implement: {_issue_title(issue)}",
            payload={
                "url": url,
                "raw": issue,
                "overlay": self.overlay_name,
                "author": issue_author(issue),
                "verdict": verdict.value,
                # A claim IS an unconditional maker-side kickoff: the shared
                # t3:orchestrator persistence handler creates the Ticket + coding
                # Task only when auto_start is True, so a claimed issue that
                # omitted this flag would strand with no task (#3100/#3213).
                "auto_start": True,
            },
        )

    def _budget_exhausted(self) -> bool:
        """True once the live in-flight count has reached ``max_concurrent``.

        Re-read per candidate rather than pre-computed: each successful claim
        records a new in-flight marker, so the live count is the authority.
        """
        if self.max_concurrent <= 0:
            return False
        return ImplementedIssueMarker.objects.in_flight_count(self.overlay_name) >= self.max_concurrent

    @staticmethod
    def _governor_denied() -> bool:
        """True when the admission governor brakes new intake this tick (F9).

        Issue intake gated only on its static ``max_concurrent`` and never asked
        the governor, yet the measured congestion collapse was on the headless
        lane the admitted issue then runs on. A DENY defers new intake with a
        visible log; fail-open (``None``) leaves intake unchanged.

        Asks with no phase, which is the EXPENSIVE class (#4098): what intake
        admits is a new coding ticket, so it is braked exactly as it was before
        the cheap-phase exemption existed.
        """
        from teatree.core.agent_admission import agent_admission_denied_reason  # noqa: PLC0415 — deferred

        reason = agent_admission_denied_reason()
        if reason is not None:
            logger.info("IssueIntakeScanner deferring new intake: governor DENIED admission: %s", reason)
        return reason is not None

    def _claim(self, url: str) -> ImplementedIssueMarker | None:
        """Claim *url*, cross-instance mutex first when the fleet kill-switch is on.

        ``None`` from the fleet acquire — a live rival holds it, or the ref infra is
        unreachable and the acquire failed safe — skips this issue.

        A live BRANCH/PR work lease on this issue also yields ``None`` (#3561):
        an interactive session that opened the PR by hand outside the lifecycle
        holds a lease the loop can now see, so the loop DEFERS instead of pushing
        divergent commits to the same branch. The deferral lapses with the lease's
        TTL, so a session that walked away never wedges the loop.
        """
        holder = foreign_work_holder(WorkIdentity(issue_url=url), owner=instance_id())
        if holder:
            logger.info("Deferring the claim of %s: %r holds its branch/PR work lease (#3561).", url, holder)
            return None
        if not wire.fleet_claim_enabled(self.overlay_name):
            return ImplementedIssueMarker.objects.claim(url, overlay=self.overlay_name)
        claim = wire.acquire_issue_claim(url)
        if claim is None:
            return None
        return ImplementedIssueMarker.objects.cache_from_fleet_claim(
            url, self.overlay_name, claim_ref_sha=claim.sha, claimed_by_instance=claim.instance_id
        )

    def _trusted_author_set(self) -> frozenset[str]:
        """The FULL trusted-author union — the config tier plus the ``TrustedIdentity`` rows."""
        config_tier = frozenset(handle.strip().lower() for handle in self.trusted_authors if handle.strip())
        return config_tier | trusted_handles()

    def _tracked_issue_urls(self) -> frozenset[str]:
        """Issue URLs a ticket already owns — the work-exists rule's local half.

        Ownership is :meth:`Ticket.issue_owning_states` (every state but IGNORED), the
        SSOT rather than a second hand-maintained list — the list this replaced omitted
        PLANNED and DELIVERED, so a parked ticket's issue was re-admitted every tick
        (#4133).

        Fails SAFE to empty: a DB-blocked harness degrades to "no local work known",
        and the forge read-back plus the TOCTOU-safe marker claim still guard against
        a double dispatch.
        """
        try:
            ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
            qs = ticket_model.objects.filter(state__in=ticket_model.issue_owning_states())
            if self.overlay_name:
                qs = qs.filter(overlay=self.overlay_name)
            return frozenset(url for url in qs.values_list("issue_url", flat=True) if url)
        except Exception:
            logger.exception("IssueIntakeScanner could not read the ticket ledger — degrading to empty")
            return frozenset()

    def _resolve_identities(self) -> tuple[str, ...]:
        """The OPERATOR's handles — the read-back's PR-query scope, not the trust set."""
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _candidate_issues(self, trusted: frozenset[str]) -> list[RawAPIDict]:
        """Open, URL-bearing issues from both scoped discovery queries, OLDEST FILED FIRST.

        The sort is over the DEDUPED UNION, not per query: each query already asks its
        forge for created-ascending, but a per-query order says nothing about the merge
        of a per-author fan-out plus the label query, and the merge is what the budget
        consumes. Sorting is stable, so issues the forge gave no filing date for keep
        their arrival order behind the dated ones.

        An app handle (any ``/``-containing handle) is skipped outright: it can never
        author a real intake, so its query is pure waste.

        Each query is fault-isolated (#3508): one identity's rate limit, deleted
        account, or transient forge error is logged and skipped, so a sibling
        identity's issues still surface this tick.
        """
        seen_urls: set[str] = set()
        issues: list[RawAPIDict] = []
        for author in sorted(trusted):
            if "/" in author:
                continue
            self._collect(
                lambda a=author: self.host.list_authored_issues(author=a, repo_slugs=self.repo_slugs),
                f"list_authored_issues({author})",
                seen_urls,
                issues,
            )
        if self.admit_label:
            self._collect(
                lambda: self.host.list_labeled_issues(label=self.admit_label, repo_slugs=self.repo_slugs),
                f"list_labeled_issues({self.admit_label})",
                seen_urls,
                issues,
            )
        return sorted(issues, key=lambda issue: issue_created_at(issue) or _UNDATED)

    @staticmethod
    def _collect(
        fetch: "Callable[[], list[RawAPIDict]]",
        label: str,
        seen_urls: set[str],
        issues: list[RawAPIDict],
    ) -> None:
        try:
            found = fetch()
        except Exception:
            logger.warning("%s failed — skipping", label, exc_info=True)
            return
        for issue in found:
            url = issue_url(issue)
            if not url or url in seen_urls or not _issue_is_open(issue):
                continue
            seen_urls.add(url)
            issues.append(issue)
