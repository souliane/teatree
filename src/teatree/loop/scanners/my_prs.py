"""Scan PRs the active user has open across configured code-host repos."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlsplit

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import RedMrFixAttempt
from teatree.core.models.review_verdict import HeadVerdictState
from teatree.core.review.mr_ci_state import GREEN_STATUSES, carries_pipeline_field, pipeline_status
from teatree.core.review.pr_inventory import PrInventoryEntry
from teatree.core.review.pr_next_action import UNKNOWN, approvals_label, approvals_text, my_pr_next_task, threads_label
from teatree.core.review.review_candidate import author_username
from teatree.loop.scanners.base import ScanSignal, SignalPayload, resolve_identities
from teatree.loop.scanners.pr_findings import (
    CriticFindingReader,
    FindingDisposition,
    ReviewVerdictReader,
    disposition_for_author,
    unaddressed_review_findings,
)
from teatree.loop.scanners.pr_payload import head_sha
from teatree.loop.url_specificity import url_claimed
from teatree.types import RawAPIDict
from teatree.utils.throttled_log import warn_throttled
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


def _known_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _str_field(data: RawAPIDict, *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str):
            return value
    return ""


def _int_field(data: RawAPIDict, *names: str) -> int:
    for name in names:
        value = data.get(name)
        if isinstance(value, int):
            return value
    return 0


# Legitimately still in progress — not green yet, but not red either. Blank
# ("") means no pipeline has started; treat that as not-yet-running, not a
# failure (a brand-new PR or a no-CI repo shouldn't spam action-needed).
_IN_PROGRESS_STATUSES = {
    "running",
    "pending",
    "created",
    "preparing",
    "waiting_for_resource",
    "scheduled",
    "",
}


#: Settled without a failure: every job of the pipeline was excluded by ``rules:``.
_SETTLED_STATUSES = GREEN_STATUSES | {"skipped"}

#: Blocked on a person playing a manual job — waiting, not broken, so no fix agent.
_MANUAL_ACTION_STATUSES = frozenset({"manual"})

MANUAL_ACTION_KIND = "my_pr.manual_action"
FAILED_KIND = "my_pr.failed"


def _needs_attention(status: str) -> bool:
    """Not-green == red, for every state that is not settled, waiting on a person, or in progress.

    ``failed``/``error``/``canceled``/``blocked``/any unknown terminal value surfaces as
    action-needed rather than passing silently as a benign open PR.
    """
    return status not in _SETTLED_STATUSES | _MANUAL_ACTION_STATUSES | _IN_PROGRESS_STATUSES


def _ci_label(kind: str, status: str) -> str:
    if status in _SETTLED_STATUSES:
        return "green"
    if kind == FAILED_KIND:
        return "red"
    if status in _IN_PROGRESS_STATUSES and status:
        return "pending"
    return status or UNKNOWN


class CiEnricher(Protocol):
    """Resolves a PR's pipeline status when its list payload never carried one.

    Implemented by :class:`teatree.loop.scanners.my_prs_ci.BoundedCiEnricher`; the
    seam is a Protocol so the scanner stays free of the forge transport and a test
    can hand it a plain callable.
    """

    def status_for(self, *, url: str, head_sha: str) -> str: ...


@dataclass(slots=True)
class MyPrsScanner:
    """Lists open PRs authored by the active user.

    Returns a ``my_pr.failed`` signal when the head pipeline is in a
    failed state, one of two review-findings signals when the head still owes
    work for a finding, and ``my_pr.open`` for every other open PR so
    the dispatcher can render an "in flight" summary.

    The two findings signals ARE the owner's fix-vs-post rule, keyed on the MR
    author: ``my_pr.findings_to_fix`` on our own (or our declared bot's) MR, which
    the routing table dispatches to the fix agent; ``my_pr.draft_notes`` on anyone
    else's — and on an author that could not be resolved — which only surfaces, so
    a colleague's branch is never written to. See
    :mod:`teatree.loop.scanners.pr_findings`.

    ``identities`` opts the scanner into a multi-alias union query — used
    when the user has more than one identity on the same forge (a personal
    login plus an org-account login under one PAT scope). When empty the
    scanner falls back to ``host.current_user()`` so legacy single-identity
    setups behave unchanged. PRs surfaced under multiple aliases are
    deduped by ``url`` (#976).

    ``allowed_url_prefixes`` gates emission to PRs whose URL starts with
    one of the listed prefixes. A scanner registered for an overlay should
    pass its workspace-repo URL prefixes here so PRs from sibling overlays
    sharing the same code-host token don't bleed into this overlay's
    statusline zone (#1015). Empty tuple keeps the legacy "emit all"
    behaviour for callers that scan a single global account.

    ``competing_url_prefixes`` carries the URL-prefix claims of OTHER
    registered overlays (#1324). When a PR's URL is claimed by both this
    overlay and another, the most-specific claim wins — a wildcard prefix
    like ``host/*/repo/`` loses to an exact ``host/owner/repo/`` claim, so
    a teatree-overlay dogfooding scan that lists ``souliane/teatree`` plus
    a sibling overlay's repo path does not steal the sibling's PRs from
    its own zone. Empty tuple disables cross-overlay attribution.

    ``ci_enricher`` supplies the pipeline status for a PR whose list payload
    carries none — the cross-project MR-list shape, where every MR would otherwise
    read as in-progress and the ``my_pr.failed`` lane is unreachable. ``None``
    keeps the payload-only behaviour. It is consulted only for PRs that survive
    ``allowed_url_prefixes``, so a forge call is never spent on a sibling
    overlay's MR.

    ``verdict_reader`` supplies the recorded cold-review verdict at the live head.
    It is what makes a findings signal fire for a HOLD the headless reviewer
    RETURNED (a HOLD often posts nothing on the forge, so the note count alone
    misses it) and stay silent for a head a reviewer already vouched for. ``None``
    degrades to the forge note count.
    """

    host: CodeHostBackend
    identities: tuple[str, ...] = field(default_factory=tuple)
    allowed_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    competing_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    ci_enricher: CiEnricher | None = None
    verdict_reader: ReviewVerdictReader | None = None
    critic_reader: CriticFindingReader | None = None
    overlay_name: str = ""
    host_origin: str = ""
    inventory_writer: Callable[[str, list[PrInventoryEntry]], object] | None = None
    inventory_invalidator: Callable[[str], object] | None = None
    name: str = "my_prs"

    def scan(self) -> list[ScanSignal]:
        authors = resolve_identities(self.host, self.identities)
        if not authors:
            return []
        prs, complete = self._collect_unique_prs(authors)
        signals: list[ScanSignal] = []
        unenriched = 0
        for pr in prs:
            url = _str_field(pr, "web_url", "html_url")
            if not self._url_allowed(url):
                continue
            title = _str_field(pr, "title")
            iid = _int_field(pr, "iid", "number")
            sha = head_sha(pr)
            status = pipeline_status(pr)
            if not carries_pipeline_field(pr):
                status = self._enriched_status(url=url, head_sha=sha)
                if not status:
                    # Neither the payload nor the live read produced CI state, so
                    # my_pr.failed can't fire for this PR. Count it and warn once
                    # per tick rather than silently classifying it as a benign open PR.
                    unenriched += 1
            base_payload: SignalPayload = {
                "url": url,
                "title": title,
                "iid": iid,
                "status": status,
                # Carried on EVERY signal so ``my_pr.failed`` reaches
                # ``claim_red_mr_fix`` with a real head sha — the RedMrFixAttempt
                # ledger stayed empty (#7) while this was omitted.
                "head_sha": sha,
                "raw": pr,
            }
            if status in _MANUAL_ACTION_STATUSES:
                signals.append(
                    ScanSignal(
                        kind=MANUAL_ACTION_KIND,
                        summary=f"PR #{iid} pipeline waiting for a manual action: {title}",
                        payload=base_payload,
                    )
                )
                continue
            if _needs_attention(status):
                signals.append(
                    ScanSignal(
                        kind=FAILED_KIND,
                        summary=f"PR #{iid} pipeline {status or 'no-status'} (not green): {title}",
                        payload=base_payload,
                    )
                )
                continue
            findings_signal = self._findings_signal(
                pr, base_payload=base_payload, title=title, iid=iid, identities=authors
            )
            if findings_signal is not None:
                signals.append(findings_signal)
                continue
            signals.append(
                ScanSignal(
                    kind="my_pr.open",
                    summary=f"PR #{iid} {status or 'open'}: {title}",
                    payload=base_payload,
                )
            )
        self._write_complete_inventory(signals, complete=complete)
        if unenriched:
            warn_throttled(
                logger,
                f"my_prs-unenriched:{self.name}",
                "%s: %d open PR(s) carry no pipeline field — the my_pr.failed auto-debug lane is inert for them; "
                "the code host did not populate CI status",
                self.name,
                unenriched,
            )
        return signals

    def _findings_signal(
        self, pr: RawAPIDict, *, base_payload: SignalPayload, title: str, iid: int, identities: tuple[str, ...]
    ) -> ScanSignal | None:
        """The signal for a PR carrying review findings nobody has acted on, or ``None``.

        Which KIND it is IS the owner's rule, and the routing table reads it
        directly: ``my_pr.findings_to_fix`` is dispatched to the fix agent (our own
        or our bot's MR), ``my_pr.draft_notes`` only surfaces (anyone else's MR, or
        an author we could not resolve). Nothing downstream re-derives the
        disposition, so there is exactly one place it can be got wrong.
        """
        notes_count = _int_field(pr, "user_notes_count", "review_comments")
        url = str(base_payload.get("url") or "")
        sha = str(base_payload.get("head_sha") or "")
        critic_unmet = self.critic_reader.unmet_for(url=url, head_sha=sha) if self.critic_reader is not None else False
        if not unaddressed_review_findings(
            verdict_state=self._verdict_state(url=url, head_sha=sha),
            notes_count=notes_count,
            critic_unmet=critic_unmet,
        ):
            return None
        author = author_username(pr)
        payload: SignalPayload = {**base_payload, "draft_count": notes_count, "author": author}
        if disposition_for_author(author, self_identities=identities) is FindingDisposition.POST:
            return ScanSignal(
                kind="my_pr.draft_notes",
                summary=f"PR #{iid} has {notes_count} unresolved notes: {title}",
                payload=payload,
            )
        # ``fix_kind`` picks the RedMrFixAttempt ledger slot AND the remedy the
        # debugging task is scheduled for — without it the dispatch would claim
        # (and be deduped against) the CI-red slot.
        payload["fix_kind"] = RedMrFixAttempt.Kind.REVIEW_FINDINGS.value
        return ScanSignal(
            kind="my_pr.findings_to_fix",
            summary=f"PR #{iid} has {notes_count} unresolved review findings to fix: {title}",
            payload=payload,
        )

    def _verdict_state(self, *, url: str, head_sha: str) -> HeadVerdictState | None:
        if self.verdict_reader is None:
            return None
        return self.verdict_reader.state_for(url=url, head_sha=head_sha)

    def _enriched_status(self, *, url: str, head_sha: str) -> str:
        if self.ci_enricher is None:
            return ""
        return self.ci_enricher.status_for(url=url, head_sha=head_sha)

    def _url_allowed(self, url: str) -> bool:
        return url_claimed(url, self.allowed_url_prefixes, self.competing_url_prefixes)

    def _write_complete_inventory(self, signals: list[ScanSignal], *, complete: bool) -> None:
        if not complete:
            self._invalidate_inventory()
            return
        if self.inventory_writer is None:
            return
        if any(not _str_field(signal.payload, "url") for signal in signals):
            logger.warning("my_prs found a PR without a URL; the prior inventory no longer counts as verified")
            self._invalidate_inventory()
            return
        entries = [self._inventory_entry(signal) for signal in signals]
        try:
            self.inventory_writer(self._inventory_key(), entries)
        except (OSError, ValueError):
            logger.warning("my_prs could not record its PR inventory; signals are unaffected", exc_info=True)

    def _invalidate_inventory(self) -> None:
        if self.inventory_invalidator is not None:
            try:
                self.inventory_invalidator(self._inventory_key())
            except (OSError, ValueError):
                logger.warning("my_prs could not invalidate its PR inventory; signals are unaffected", exc_info=True)

    def _forge_review_facts(self, url: str) -> tuple[str, str, str]:
        ref = pr_ref_from_url(url)
        if ref is None:
            return UNKNOWN, UNKNOWN, UNKNOWN
        try:
            state = self.host.get_mr_approvals(repo=ref.slug, pr_iid=ref.pr_id)
            label = approvals_label(state["approvals_left"])
            return (
                label,
                threads_label(state["unresolved_resolvable"]),
                approvals_text(label, _known_count(state.get("count")), _known_count(state.get("required"))),
            )
        except Exception:
            logger.warning("my_prs could not read approvals for %s", url, exc_info=True)
            return UNKNOWN, UNKNOWN, UNKNOWN

    def _inventory_entry(self, signal: ScanSignal) -> PrInventoryEntry:
        payload = signal.payload
        raw = payload.get("raw")
        pr = raw if isinstance(raw, dict) else {}
        url = _str_field(payload, "url")
        ref = pr_ref_from_url(url)
        status = _str_field(payload, "status")
        ci = _ci_label(signal.kind, status)
        approvals, threads, approval_display = (
            self._forge_review_facts(url) if ci == "green" else (UNKNOWN, UNKNOWN, UNKNOWN)
        )
        return PrInventoryEntry(
            url=url,
            repo=ref.slug if ref else "unknown repo",
            iid=ref.pr_id if ref else _int_field(payload, "iid"),
            title=_str_field(payload, "title"),
            created_at=_str_field(pr, "created_at", "createdAt"),
            updated_at=_str_field(pr, "updated_at", "updatedAt"),
            next_task=my_pr_next_task(signal.kind, ci),
            decision=signal.kind,
            ci=ci,
            approvals=approvals,
            approvals_display=approval_display,
            threads=threads,
            draft=bool(pr.get("draft")),
            head_sha=_str_field(payload, "head_sha"),
            author=author_username(pr),
            owned=True,
        )

    def _inventory_key(self) -> str:
        prefixes = "|".join(sorted(self.allowed_url_prefixes))
        first = urlsplit(self.allowed_url_prefixes[0]) if self.allowed_url_prefixes else None
        origin = self.host_origin or (f"{first.scheme}://{first.netloc}" if first and first.netloc else "")
        host_key = origin or f"{type(self.host).__module__}.{type(self.host).__qualname__}"
        host_hash = sha256(host_key.encode()).hexdigest()[:16]
        scope_hash = sha256((prefixes or host_key).encode()).hexdigest()[:16]
        return f"own:{self.overlay_name}:{host_hash}:{scope_hash}"

    def _collect_unique_prs(self, authors: tuple[str, ...]) -> tuple[list[RawAPIDict], bool]:
        """Union PRs across *authors*, deduped by URL.

        A PR returned for two aliases (co-author / shared identity) renders
        once. PRs without a URL keep their legacy "emit once" shape — there
        is no other stable identity to dedup on.
        """
        seen_urls: set[str] = set()
        prs: list[RawAPIDict] = []
        complete = True
        for author in authors:
            try:
                fetched = self.host.list_my_prs(author=author)
            except Exception:
                logger.warning(
                    "list_my_prs failed for %s — keeping the prior complete inventory", author, exc_info=True
                )
                complete = False
                continue
            for pr in fetched:
                url = _str_field(pr, "web_url", "html_url")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                prs.append(pr)
        return prs, complete
