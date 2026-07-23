"""Discover open issues filed by a TRUSTED AUTHOR and claim them for auto-implementation (#1553, #3235).

The always-on issue-implementer loop (default-OFF behind the
``issue_implementer_enabled`` gate) picks up issues and dispatches them into the
autonomous pipeline. #3235 moved INTAKE off the hand-applied
``issue_implementer_label`` and onto the issue's AUTHOR: the owner does not tag
tickets, so the trusted author IS the authority. Every open issue filed by a
trusted human is claimable, with no label, no triage, and no assignment.

That makes the author gate a SAFETY boundary. On a public repo, anyone can file an
issue, so the trusted-author set is the only thing between a stranger and the
autonomous factory — and the gate is therefore FAIL-CLOSED in every direction:

* Candidate selection queries the forge author-scoped, once per trusted author, so
    a stranger's issue is never even fetched.
* :func:`_author_is_trusted` then RE-CHECKS every issue at claim time.
    Selection narrows; the gate decides. An issue that surfaces by any
    other route — a forge query that over-returns, a payload naming a different
    author than the query it arrived under, a future backend that widens the scope —
    is refused outright: no signal, no marker, no dispatch.
* An unresolvable author, an unparsable issue URL, and an empty trusted set all
    resolve to REFUSE, never to "allow".

Trust resolution reuses :func:`~teatree.core.review.author_trust.classify_author` —
the same seam the merge keystone and the reviewing scanners consume — so the
factory cannot hold two different opinions about who a trusted human is.

This governs INTAKE only. Merge authority is untouched: a substrate PR still needs
a recorded human approver, whoever filed the issue that spawned it.

Whether the scanner runs at all is decided one layer up by
:func:`teatree.loop.scanner_factories._issue_implementer_scanner_for` — the triple
gate (enabled flag, in-flight concurrency budget, per-issue claim idempotency).
"""

import logging
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlparse

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.fleet import wire
from teatree.core.intake.admission_policy import admit_issue
from teatree.core.models import NEEDS_TRIAGE_LABEL, ImplementedIssueMarker
from teatree.core.review.author_trust import classify_author, is_trusted_author, trusted_handles
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.forge_readback import existing_work_for_issue, fetch_merged_prs, fetch_open_prs, issue_number
from teatree.types import RawAPIDict
from teatree.utils.url_slug import slug_from_issue_or_pr_url

logger = logging.getLogger(__name__)


def _issue_url(issue: RawAPIDict) -> str:
    for name in ("web_url", "html_url"):
        value = issue.get(name)
        if isinstance(value, str):
            return value
    return ""


def _issue_title(issue: RawAPIDict) -> str:
    title = issue.get("title")
    return title if isinstance(title, str) else ""


def _issue_author(issue: RawAPIDict) -> str:
    """The handle that FILED *issue*, across both forges' payload shapes.

    GitHub nests the author under ``user.login``; GitLab under ``author.username``.
    Returns ``""`` when no author can be resolved — which the gate reads as UNTRUSTED,
    never as a wildcard: an issue whose author teatree cannot name is an issue teatree
    must not act on.
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


def _issue_labels(issue: RawAPIDict) -> list[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    out: list[str] = []
    for item in labels:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            name = cast("RawAPIDict", item).get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def _issue_is_open(issue: RawAPIDict) -> bool:
    """Treat an issue as open unless the backend explicitly reports otherwise.

    The forge issue-search the backends use already filters to open issues
    (``is:open`` / ``state=opened``), so a missing ``state`` is open by
    construction; a present ``state`` of ``closed`` is the only thing that
    excludes an issue.
    """
    state = issue.get("state")
    return not (isinstance(state, str) and state.lower() == "closed")


def _issue_slug_and_host_kind(url: str) -> tuple[str, str]:
    """The ``(repo_slug, host_kind)`` the author classifier needs, from an issue URL.

    The slug comes from the shared :func:`slug_from_issue_or_pr_url` parser (one
    slug-extraction mechanism, reused). ``host_kind`` routes the classifier's
    visibility probe to the right forge CLI: the GitLab ``/-/`` path shape is
    definitive, with the hostname as the fallback tell. An unrecognised URL yields an
    EMPTY slug, which the gate refuses — an unclassifiable issue is never claimable.
    """
    parsed = urlparse(url)
    slug = slug_from_issue_or_pr_url(parsed.path)
    is_gitlab = "/-/" in parsed.path or "gitlab" in (parsed.hostname or "").lower()
    return slug, "gitlab" if is_gitlab else "github"


def _author_is_trusted(issue: RawAPIDict, trusted: frozenset[str]) -> bool:
    """The fail-closed per-issue author gate — REFUSE unless the filer is a named trusted human.

    Two conjuncts, both from the shared :mod:`~teatree.core.review.author_trust` seam,
    because intake is strictly stricter than merge:

    * :func:`classify_author` is the seam the merge keystone and the reviewing scanners
        share, so the factory holds ONE opinion of who is trusted. On a public repo it is
        the decision.
    * :func:`is_trusted_author` additionally requires EXPLICIT membership of the trusted
        set. This is what closes the internal-repo bypass: ``classify_author`` calls every
        author on a PRIVATE repo trusted (the user owns access control there) — the right
        call for judging a merge, far too loose for INTAKE, where it would let any repo
        collaborator command the autonomous factory just by filing an issue. Membership is
        required no matter the repo's visibility.

    An issue with no resolvable author, or no resolvable repo slug, is refused: an
    unclassifiable issue is never claimable.
    """
    author = _issue_author(issue)
    slug, host_kind = _issue_slug_and_host_kind(_issue_url(issue))
    if not author or not slug:
        return False
    classification = classify_author(slug, author, host_kind=host_kind, extra_trusted=trusted)
    return classification.trusted and is_trusted_author(author, extra_trusted=trusted)


@dataclass(slots=True)
class IssueImplementerScanner:
    """Claim open issues filed by a TRUSTED author for the auto-implementer pipeline (#3235).

    Queries *host* author-scoped for each handle in the resolved trusted set, refuses
    every issue whose author is not trusted (see the module docstring — this is the
    fail-closed safety gate), drops the ones carrying :data:`NEEDS_TRIAGE_LABEL` (a
    maintainer-applied HOLD that survives #3235 untouched), and claims the rest via the
    TOCTOU-safe :meth:`ImplementedIssueMarker.claim`. A claim that returns ``None``
    (the row already exists — another tick or overlay took it) is skipped silently, so
    the scanner never double-dispatches. Each newly claimed issue surfaces one
    ``issue_implementer.claimed`` signal, which the C4 dispatch layer routes into the
    implementation pipeline.

    ``trusted_authors`` is the CONFIG tier of the trust union — the owner's
    ``user_identity_aliases`` plus the ``trusted_issue_authors`` allowlist, already
    normalised by :func:`teatree.config.effective_trusted_issue_authors`. The DB tier
    (the ``TrustedIdentity`` rows) is unioned in here, where the DB is reachable. An
    EMPTY union claims NOTHING: no trusted author means no intake, never "trust all".

    ``require_label`` (default False) restores the pre-#3235 ``label`` filter as a
    MANDATORY second gate, for an operator who wants to keep hand-tagging. It can only
    ever NARROW intake — the author gate is not optional and a label can never launder
    an untrusted author.

    ``identities`` is the OPERATOR's own handle set. It is deliberately NOT the trust
    set: it scopes the read-back's PR queries, because the PR that implements an issue
    is authored by the operator regardless of who filed the issue.

    ``readback_enabled`` (default on) runs the pre-dispatch forge read-back
    (:func:`~teatree.loop.scanners.forge_readback.existing_work_for_issue`) before each
    claim: an issue whose branch cites the ticket number, or which an open/merged PR
    already references, is skipped — closing most of the cross-instance double-claim
    window the local claim ledger cannot see.
    """

    host: CodeHostBackend
    label: str
    overlay_name: str = ""
    trusted_authors: tuple[str, ...] = field(default_factory=tuple)
    require_label: bool = False
    identities: tuple[str, ...] = field(default_factory=tuple)
    #: The overlay's OWN repo slugs (``owner/name``). Every author query is scoped to
    #: them, so a trusted human's issue on a repo the factory does not own is never
    #: fetched — closing both the cross-repo firehose and the cross-repo claim hole.
    #: Empty keeps the pre-scope global author search (back-compat).
    repo_slugs: tuple[str, ...] = field(default_factory=tuple)
    name: str = "issue_implementer"
    readback_enabled: bool = True
    #: The single-ticket in-flight budget. The factory gate decides whether the
    #: scanner runs; this caps how many NEW issues one tick may claim, so a full
    #: backlog is not claimed all at once. 0 means uncapped (the loop claims every
    #: candidate) — the safe default for a directly-constructed scanner.
    max_concurrent: int = 0
    #: When False (budget full, or require_label with no label) this tick only
    #: HEARTBEATS in-flight fleet claims and claims no new issue — the heartbeat must
    #: run even at full budget or an in-flight claim would expire mid-dispatch
    #: (fleet-safety Stage 2).
    can_claim: bool = True

    def scan(self) -> list[ScanSignal]:
        # Stage 2 B1: keep every in-flight claim un-stealable, on EVERY tick,
        # regardless of budget/label (self-gates to a no-op when the switch is off).
        wire.heartbeat_inflight_claims(self.overlay_name)
        if not self.can_claim:
            return []
        trusted = self._trusted_author_set()
        if not trusted:
            logger.warning(
                "issue-implementer loop enabled for overlay %r but NO trusted issue author resolves "
                "(user_identity_aliases, trusted_issue_authors, and TrustedIdentity are all empty) — "
                "nothing will be dispatched until a trusted author is configured",
                self.overlay_name,
            )
            return []
        operators = self._resolve_identities()
        candidates = self._candidate_issues(trusted, operators)
        if not candidates:
            return []
        open_prs = fetch_open_prs(self.host, authors=operators) if self.readback_enabled else []
        merged_prs = fetch_merged_prs(self.host, authors=operators) if self.readback_enabled else []
        signals: list[ScanSignal] = []
        for issue in candidates:
            if self._budget_exhausted():
                break
            url = _issue_url(issue)
            try:
                hit = existing_work_for_issue(
                    issue_url=url,
                    ticket_number=issue_number(url),
                    open_prs=open_prs,
                    merged_prs=merged_prs,
                )
                if hit is not None:
                    logger.info(
                        "IssueImplementerScanner read-back skip %s: %s (%s)",
                        url,
                        hit.reason,
                        hit.evidence_url,
                    )
                    continue
                marker = self._claim(url)
                if marker is None:
                    continue
                signals.append(
                    ScanSignal(
                        kind="issue_implementer.claimed",
                        summary=f"Claimed for auto-implement: {_issue_title(issue)}",
                        payload={
                            "url": url,
                            "raw": issue,
                            "overlay": self.overlay_name,
                            "author": _issue_author(issue),
                            # A claim IS an unconditional maker-side kickoff: the
                            # triple gate (_issue_implementer_scanner_for) already
                            # enforced enablement + concurrency budget, the author gate
                            # cleared the filer, and the TOCTOU-safe marker claim just
                            # committed this issue. The shared t3:orchestrator
                            # persistence handler (_handle_orchestrator) creates the
                            # Ticket + coding Task only when auto_start is True, so a
                            # claimed issue that omitted this flag dispatched an agent
                            # action that was then silently dropped at persist time —
                            # the claim stranded with no task (#3100/#3213).
                            "auto_start": True,
                        },
                    )
                )
            except Exception:
                logger.exception("IssueImplementerScanner failed on issue %s", url)
                continue
        return signals

    def _budget_exhausted(self) -> bool:
        """True once the live in-flight count has reached ``max_concurrent``.

        Re-read per candidate rather than pre-computed: each successful claim
        records a new in-flight marker, so the live count is the authority — a
        read-back skip or an already-claimed issue consumes no budget.
        """
        if self.max_concurrent <= 0:
            return False
        return ImplementedIssueMarker.objects.in_flight_count(self.overlay_name) >= self.max_concurrent

    def _claim(self, url: str) -> ImplementedIssueMarker | None:
        """Claim *url*, cross-instance mutex first when the kill-switch is on.

        Kill-switch OFF (default): today's local ``get_or_create`` claim.
        ON: win the GitHub claim ref FIRST (fleet-safety Stage 2). ``None`` there —
        a live rival holds it, or the ref infra is unreachable and the acquire
        failed safe — skips this issue (do not dispatch). On a win the local marker
        is recorded as a CACHE of the ref, stamped with the fencing sha the ship
        gate re-verifies.
        """
        if not wire.fleet_claim_enabled(self.overlay_name):
            return ImplementedIssueMarker.objects.claim(url, overlay=self.overlay_name)
        claim = wire.acquire_issue_claim(url)
        if claim is None:
            return None
        return ImplementedIssueMarker.objects.cache_from_fleet_claim(
            url, self.overlay_name, claim_ref_sha=claim.sha, claimed_by_instance=claim.instance_id
        )

    def _trusted_author_set(self) -> frozenset[str]:
        """The FULL trusted-author union — the config tier unioned with the canonical ``TrustedIdentity`` rows.

        :func:`~teatree.core.review.author_trust.trusted_handles` supplies the DB tier
        (with its own documented config fallback for the pre-migration window);
        ``trusted_authors`` supplies the config tier the DB cannot see. Both are
        lower-cased, so the union is directly comparable to a normalised author handle.
        """
        config_tier = frozenset(handle.strip().lower() for handle in self.trusted_authors if handle.strip())
        return config_tier | trusted_handles()

    def _candidate_issues(self, trusted: frozenset[str], operators: tuple[str, ...]) -> list[RawAPIDict]:
        """Open, URL-bearing, TRUSTED-AUTHOR, ADMITTED issues not held by :data:`NEEDS_TRIAGE_LABEL`.

        The claimable set, resolved before the per-issue read-back + claim loop so the
        forge open-PR fetch only fires when there is real work to guard. The label
        filter applies ONLY under ``require_label`` — by default an unlabelled issue
        from a trusted author is exactly what this loop exists to pick up. The
        per-overlay admission policy (:func:`admit_issue`, keyed on the operator's own
        *operators* handles) runs LAST, so a rejected issue is dropped before it can
        be claimed — the marker/budget is never spent on work the overlay will not do.
        """
        candidates: list[RawAPIDict] = []
        for issue in self._collect_unique_issues(trusted):
            if not _issue_is_open(issue):
                continue
            if not _issue_url(issue):
                continue
            labels = _issue_labels(issue)
            if NEEDS_TRIAGE_LABEL in labels:
                continue
            if self.require_label and self.label not in labels:
                continue
            if not _author_is_trusted(issue, trusted):
                logger.info(
                    "IssueImplementerScanner REFUSED %s: author %r is not a trusted issue author",
                    _issue_url(issue),
                    _issue_author(issue),
                )
                continue
            if not admit_issue(issue, overlay=self.overlay_name, owner_handles=operators):
                logger.info(
                    "IssueImplementerScanner REFUSED %s: overlay %r admission_policy rejects it for autonomous work",
                    _issue_url(issue),
                    self.overlay_name,
                )
                continue
            candidates.append(issue)
        return candidates

    def _resolve_identities(self) -> tuple[str, ...]:
        """The OPERATOR's handles — the read-back's PR-query scope, not the trust set."""
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _collect_unique_issues(self, trusted: frozenset[str]) -> list[RawAPIDict]:
        """Union each TRUSTED author's open issues, deduped by URL.

        Author- AND repo-scoped by construction: the forge is asked ONLY about the humans
        in the trusted union, each query bound to :attr:`repo_slugs` (the overlay's own
        repos), so a stranger's issue — and a trusted human's issue on a repo the factory
        does not own — is never fetched at all. An app handle (``app/github-actions``, any
        ``/``-containing handle) is skipped outright: it can never author a real intake, so
        its query is pure waste (and, unscoped, the 1000-result cross-repo firehose). Sorted
        so the query fan-out (and hence the claim order under a tight concurrency budget) is
        deterministic across ticks.
        """
        seen_urls: set[str] = set()
        issues: list[RawAPIDict] = []
        for author in sorted(trusted):
            # An app handle (``app/github-actions``) can never author a real intake —
            # its ``author:`` query is pure waste (and, unscoped, a firehose). Skip it.
            if "/" in author:
                continue
            try:
                fetched = self.host.list_authored_issues(author=author, repo_slugs=self.repo_slugs)
            except Exception:
                logger.warning("list_authored_issues failed for %s — skipping", author, exc_info=True)
                continue
            for issue in fetched:
                url = _issue_url(issue)
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                issues.append(issue)
        return issues
