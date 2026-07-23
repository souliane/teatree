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
    requires the owner's explicit label (rule 4).

Selection narrows; the decision function decides. Every candidate is re-checked
at claim time through the shared :mod:`~teatree.core.review.author_trust` seam,
so an over-returning forge query cannot launder an untrusted author past rule 5.

Claims go through the TOCTOU-safe :meth:`ImplementedIssueMarker.claim` (or the
cross-instance fleet ref when that kill-switch is on), so a re-tick or a
concurrent overlay never double-dispatches.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from django.apps import apps

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.fleet import wire
from teatree.core.intake.factory_admission import decide_issue_intake
from teatree.core.models import ImplementedIssueMarker
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
from teatree.loop.scanners.forge_readback import existing_work_for_issue, fetch_merged_prs, fetch_open_prs, issue_number
from teatree.types import RawAPIDict
from teatree.utils.url_slug import slug_from_issue_or_pr_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _TickContext:
    """The per-tick facts every candidate is decided against."""

    tracked: frozenset[str]
    trusted: frozenset[str]
    open_prs: list[RawAPIDict]
    merged_prs: list[RawAPIDict]


#: A ticket in one of these states OWNS its issue URL — rule 2 of the decision
#: table reads this as "work already exists", so no second ticket is created.
_ACTIVE_TICKET_STATES: frozenset[str] = frozenset(
    {
        "not_started",
        "scoped",
        "started",
        "coded",
        "tested",
        "reviewed",
        "shipped",
        "in_review",
        "merged",
        "retrospected",
    }
)


def issue_url(issue: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_title(issue: RawAPIDict) -> str:
    title = issue.get("title")
    return title if isinstance(title, str) else ""


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
    rule 4 of the decision table — an untrusted author's issue enters only through
    it.

    ``trusted_authors`` is the CONFIG tier of the trust union (the owner's
    ``user_identity_aliases`` plus the ``trusted_issue_authors`` allowlist); the DB
    tier (``TrustedIdentity`` rows) is unioned in here. An EMPTY union trusts
    NOBODY — the label-scoped query still runs, so an owner-labelled issue is still
    admissible.

    ``identities`` is the OPERATOR's own handle set. It is deliberately NOT the
    trust set: it scopes the read-back's PR queries, because the PR implementing an
    issue is authored by the operator regardless of who filed the issue.
    """

    host: CodeHostBackend
    admit_label: str
    overlay_name: str = ""
    trusted_authors: tuple[str, ...] = field(default_factory=tuple)
    identities: tuple[str, ...] = field(default_factory=tuple)
    #: The overlay's OWN repo slugs (``owner/name``). Every discovery query is
    #: scoped to them. Empty keeps the pre-scope global search (back-compat).
    repo_slugs: tuple[str, ...] = field(default_factory=tuple)
    name: str = "issue_intake"
    readback_enabled: bool = True
    #: The single-ticket in-flight budget; 0 means uncapped.
    max_concurrent: int = 0
    #: When False this tick only HEARTBEATS in-flight fleet claims and claims
    #: nothing new — the heartbeat must run even at full budget or an in-flight
    #: claim would expire mid-dispatch.
    can_claim: bool = True

    def scan(self) -> list[ScanSignal]:
        wire.heartbeat_inflight_claims(self.overlay_name)
        if not self.can_claim:
            return []
        trusted = self._trusted_author_set()
        operators = self._resolve_identities()
        candidates = self._candidate_issues(trusted)
        if not candidates:
            return []
        open_prs = fetch_open_prs(self.host, authors=operators) if self.readback_enabled else []
        merged_prs = fetch_merged_prs(self.host, authors=operators) if self.readback_enabled else []
        context = _TickContext(
            tracked=self._tracked_issue_urls(),
            trusted=trusted,
            open_prs=open_prs,
            merged_prs=merged_prs,
        )
        signals: list[ScanSignal] = []
        for issue in candidates:
            if self._budget_exhausted():
                break
            url = issue_url(issue)
            try:
                signal = self._signal_for(issue, url, context=context)
            except Exception:
                logger.exception("IssueIntakeScanner failed on issue %s", url)
                continue
            if signal is not None:
                signals.append(signal)
        return signals

    def _signal_for(self, issue: RawAPIDict, url: str, *, context: "_TickContext") -> ScanSignal | None:
        """Decide *issue*, claim it when the verdict acts, and build its signal.

        Rule 2's "work exists" fact is the union of the local ticket ledger and the
        forge read-back, so a cross-instance PR that already cites the issue is seen
        even though no local row exists.
        """
        work_exists = bool(url) and url in context.tracked
        readback_reason = ""
        if not work_exists and self.readback_enabled:
            hit = existing_work_for_issue(
                issue_url=url,
                ticket_number=issue_number(url),
                open_prs=context.open_prs,
                merged_prs=context.merged_prs,
            )
            if hit is not None:
                work_exists = True
                readback_reason = f"{hit.reason} ({hit.evidence_url})"
        verdict = decide_issue_intake(
            issue,
            author_trusted=author_is_trusted(issue, context.trusted),
            work_exists=work_exists,
            admit_label=self.admit_label,
        )
        if not verdict.acts:
            logger.info(
                "IssueIntakeScanner %s %s (author %r)%s",
                verdict.value,
                url,
                issue_author(issue),
                f": {readback_reason}" if readback_reason else "",
            )
            return None
        marker = self._claim(url)
        if marker is None:
            return None
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
        """Issue URLs an ACTIVE ticket already owns — rule 2's local half.

        Fails SAFE to empty: a DB-blocked harness degrades to "no local work known",
        and the forge read-back plus the TOCTOU-safe marker claim still guard against
        a double dispatch.
        """
        try:
            ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
            qs = ticket_model.objects.filter(state__in=_ACTIVE_TICKET_STATES)
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
        """Open, URL-bearing issues from both scoped discovery queries, deduped by URL.

        An app handle (any ``/``-containing handle) is skipped outright: it can never
        author a real intake, so its query is pure waste. Authors are sorted so the
        query fan-out — and hence the claim order under a tight budget — is
        deterministic across ticks.

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
        return issues

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
