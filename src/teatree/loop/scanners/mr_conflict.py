"""Surface every one of the operator's open merge requests that conflicts with its target.

The rule this serves is unconditional: a merge request owes a resolved conflict
whatever its review policy says, so the walk deliberately applies NO filter that
other surfaces apply. A draft is included — it will have to merge eventually, and
conflicts only get worse while it waits. A review-exempt repo is included —
exemption governs whether a colleague is ASKED to review, which says nothing
about whether the branch still merges. Filtering either one out is how the rule
quietly stops applying to most of the merge requests it covers.

The remedy is a merge of the target branch into the head, never a rebase, and it
already exists (``t3 <overlay> pr sweep``); this module is only the detection and
the dispatch of it.

Conflict state is three-valued (:class:`MergeConflictState`), and the UNKNOWN
value is emitted rather than dropped. Both forges compute mergeability
asynchronously, so an unanswered probe is routine — but it is not evidence of
anything. Reporting it as CONFLICTED dispatches a fix for a healthy branch;
swallowing it as CLEAN leaves the operator believing a merge request was checked
when it never was, which is exactly how a probe that answers the benign value on
failure disarms the sweep it belongs to. So it emits its own kind, visible to the
operator and routed to no agent.
"""

import logging
from dataclasses import dataclass, field

from teatree.core.backend_protocols import CodeHostBackend, MergeConflictState
from teatree.loop.scanners.base import ScanSignal, SignalPayload
from teatree.loop.scanners.my_prs import _str_field
from teatree.loop.scanners.pr_payload import head_sha
from teatree.loop.url_specificity import best_url_match_specificity
from teatree.types import RawAPIDict
from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

CONFLICTED_KIND = "my_pr.conflicted"
UNKNOWN_KIND = "my_pr.conflict_unknown"

#: Stamped on the conflicted payload so the shared ``RedMrFixAttempt`` ledger and
#: the debugging-task reason resolve to the conflict remedy rather than the CI one.
FIX_KIND = "merge_conflict"


@dataclass(slots=True)
class MrConflictScanner:
    """Emit one signal per open authored merge request that does not merge cleanly.

    ``identities`` is the operator's alias set on this host, unioned and deduped
    by url exactly as the sibling PR scanners do; empty falls back to
    ``host.current_user()``. ``allowed_url_prefixes`` narrows what this overlay
    claims so a sibling overlay's merge requests stay in the sibling's zone;
    empty claims everything the host lists. ``overlay_name`` tags the emitted
    summaries.

    One forge read per surviving merge request, which is why the whole scanner is
    gated default-OFF one layer up
    (:func:`teatree.loop.scanner_factories._mr_conflict_scanner_for`).
    """

    host: CodeHostBackend
    identities: tuple[str, ...] = field(default_factory=tuple)
    allowed_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    overlay_name: str = ""
    name: str = "mr_conflict"

    def scan(self) -> list[ScanSignal]:
        authors = self._resolve_identities()
        if not authors:
            return []
        signals: list[ScanSignal] = []
        for url, pr in self._collect(authors).items():
            if not self._url_allowed(url):
                continue
            conflict = self._conflict_state(url)
            if conflict is MergeConflictState.CLEAN:
                continue
            signals.append(self._signal(conflict, url=url, pr=pr))
        return signals

    @staticmethod
    def _signal(conflict: MergeConflictState, *, url: str, pr: RawAPIDict) -> ScanSignal:
        title = _str_field(pr, "title")
        payload: SignalPayload = {
            "url": url,
            "pr_url": url,
            "title": title,
            "head_sha": head_sha(pr),
            "fix_kind": FIX_KIND,
        }
        if conflict is MergeConflictState.CONFLICTED:
            return ScanSignal(
                kind=CONFLICTED_KIND,
                summary=f"MR conflicts with its target branch — merge it in, never rebase: {title}",
                payload=payload,
            )
        return ScanSignal(
            kind=UNKNOWN_KIND,
            summary=f"MR merge state unreadable — conflicts neither confirmed nor ruled out: {title}",
            payload=payload,
        )

    def _conflict_state(self, url: str) -> MergeConflictState:
        """The forge's conflict verdict for *url*, or UNKNOWN when it cannot be had.

        An unparsable url and a raising probe are both "we did not get an answer",
        so they land on the same value the forge itself uses while it is still
        computing — never on CLEAN, which would silently exempt the merge request.
        """
        ref = pr_ref_from_url(url)
        if ref is None:
            return MergeConflictState.UNKNOWN
        return self._probe(ref, url=url)

    def _probe(self, ref: PrRef, *, url: str) -> MergeConflictState:
        try:
            return self.host.fetch_pr_merge_state(slug=ref.slug, pr_id=ref.pr_id).conflict
        except Exception as exc:  # noqa: BLE001 — a merge-state probe must never abort the tick.
            logger.warning("%s: merge-state probe failed for %s: %s", self.name, url, exc)
            return MergeConflictState.UNKNOWN

    def _url_allowed(self, url: str) -> bool:
        if not self.allowed_url_prefixes:
            return bool(url)
        return bool(url) and best_url_match_specificity(url, self.allowed_url_prefixes) > 0

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _collect(self, authors: tuple[str, ...]) -> dict[str, RawAPIDict]:
        collected: dict[str, RawAPIDict] = {}
        for author in authors:
            try:
                fetched = self.host.list_my_prs(author=author)
            except Exception:
                logger.warning("%s: list_my_prs failed for %s — skipping", self.name, author, exc_info=True)
                continue
            for pr in fetched:
                if url := _str_field(pr, "web_url", "html_url"):
                    collected.setdefault(url, pr)
        return collected


__all__ = ["CONFLICTED_KIND", "FIX_KIND", "UNKNOWN_KIND", "MrConflictScanner"]
