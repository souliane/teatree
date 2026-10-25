from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.utils import timezone

from teatree.core.backend_protocols import HEAD_SHA_UNREADABLE, CodeHostBackend
from teatree.core.merge import normalize_repo_slug
from teatree.core.models import MergeAudit
from teatree.loop.scanners.base import PartialScanError, ScannerError, ScannerErrorClass, ScanSignal
from teatree.loop.scanners.pr_sweep_decision import head_review_state
from teatree.types import RawAPIDict

WINDOW = timedelta(hours=48)
RECORD_GRACE = timedelta(minutes=3)


def _parsed(stamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(slots=True)
class UnauditedMergeScanner:
    host: CodeHostBackend
    repos: tuple[str, ...]
    overlay: str
    identities: tuple[str, ...]
    solo_overlay: bool
    name: str = "unaudited_merge"

    def scan(self, *, now: datetime | None = None) -> list[ScanSignal]:
        current = now or timezone.now()
        since = (current - WINDOW).isoformat(timespec="seconds")
        signals: list[ScanSignal] = []
        failures: list[str] = []
        for repo in self.repos:
            slug = normalize_repo_slug(repo)
            try:
                merged = self._read(slug, self.host.list_merged_prs_since, repo=slug, since=since)
            except ScannerError as exc:
                failures.append(exc.detail)
                continue
            for raw in merged:
                try:
                    signal = self._assess(slug, raw, now=current)
                except ScannerError as exc:
                    failures.append(exc.detail)
                else:
                    if signal:
                        signals.append(signal)
        if failures:
            raise PartialScanError(scanner=self.name, detail="; ".join(failures), signals=signals)
        return signals

    def _assess(self, slug: str, raw: RawAPIDict, *, now: datetime) -> ScanSignal | None:
        pr_id = raw.get("number") or raw.get("iid")
        url = raw.get("html_url") or raw.get("web_url")
        if not isinstance(pr_id, int) or not isinstance(url, str) or not url:
            raise self._unreadable(detail=f"{slug}: malformed merged PR entry")
        if MergeAudit.objects.filter(repo_slug__iexact=slug, clear__pr_id=pr_id).exists():
            return None
        ref = f"{slug}#{pr_id}"
        state = self._read(ref, self.host.fetch_pr_merge_state, slug=slug, pr_id=pr_id)
        sha = state.merge_commit_oid.strip().lower()
        merged_at = _parsed(state.merged_at)
        if not state.is_merged or not sha or merged_at is None:
            raise self._unreadable(detail=f"{ref}: merge state unreadable")
        if MergeAudit.objects.filter(merged_sha__iexact=sha).exists() or now - merged_at < RECORD_GRACE:
            return None
        if not (self.solo_overlay or state.merged_by.lower() in {identity.lower() for identity in self.identities}):
            return None
        head = self._read(ref, self.host.fetch_live_head_sha, slug=slug, pr_id=pr_id)
        if not head or head == HEAD_SHA_UNREADABLE:
            raise self._unreadable(detail=f"{ref}: head unreadable")
        review = head_review_state(slug=slug, pr_id=pr_id, head_sha=head)
        return ScanSignal(
            kind="merge.unaudited",
            summary=f"ALERT: {ref} merged without an audit; confirm intent or revert",
            payload={
                "slug": slug,
                "pr_id": pr_id,
                "url": url,
                "merged_sha": sha,
                "merged_by": state.merged_by or "unknown",
                "merged_at": state.merged_at,
                "independent_review_at_head": bool(review.standing_merge_safe),
                "hold_at_head": bool(review.held_verdicts),
                "overlay": self.overlay,
            },
        )

    def _read[T](self, label: str, read: Callable[..., T], **arguments: object) -> T:
        try:
            return read(**arguments)
        except Exception as exc:
            raise self._unreadable(detail=f"{label}: {exc}") from exc

    def _unreadable(self, *, detail: str) -> ScannerError:
        return ScannerError(scanner=self.name, error_class=ScannerErrorClass.UNKNOWN, detail=detail)
