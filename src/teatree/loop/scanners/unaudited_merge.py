from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from teatree.core.backend_protocols import HEAD_SHA_UNREADABLE, CodeHostBackend
from teatree.core.merge import normalize_repo_slug
from teatree.core.models import MergeAudit
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal
from teatree.loop.scanners.pr_sweep_decision import head_review_state


@dataclass(slots=True)
class UnauditedMergeScanner:
    host: CodeHostBackend
    repos: tuple[str, ...]
    overlay: str
    name: str = "unaudited_merge"

    def scan(self, *, now: datetime | None = None) -> list[ScanSignal]:
        current = now or timezone.now()
        since = current - timedelta(hours=48)
        signals: list[ScanSignal] = []
        for repo in self.repos:
            slug = normalize_repo_slug(repo)
            try:
                merged = self.host.list_merged_prs_since(repo=slug, since=since.isoformat(timespec="seconds"))
            except Exception as exc:
                raise ScannerError(
                    scanner=self.name, error_class=ScannerErrorClass.UNKNOWN, detail=f"{slug}: {exc}"
                ) from exc
            for raw in merged:
                pr_id = raw.get("number") or raw.get("iid")
                url = raw.get("html_url") or raw.get("web_url")
                if not isinstance(pr_id, int) or not isinstance(url, str) or not url:
                    raise ScannerError(
                        scanner=self.name,
                        error_class=ScannerErrorClass.UNKNOWN,
                        detail=f"{slug}: malformed merged PR entry",
                    )
                try:
                    state = self.host.fetch_pr_merge_state(slug=slug, pr_id=pr_id)
                    head = self.host.fetch_live_head_sha(slug=slug, pr_id=pr_id)
                except Exception as exc:
                    raise ScannerError(
                        scanner=self.name, error_class=ScannerErrorClass.UNKNOWN, detail=f"{slug}#{pr_id}: {exc}"
                    ) from exc
                sha = state.merge_commit_oid.strip().lower()
                if not state.is_merged or not sha or not head or head == HEAD_SHA_UNREADABLE:
                    raise ScannerError(
                        scanner=self.name,
                        error_class=ScannerErrorClass.UNKNOWN,
                        detail=f"{slug}#{pr_id}: merge state or head unreadable",
                    )
                if MergeAudit.objects.filter(merged_sha__iexact=sha).exists():
                    continue
                review = head_review_state(slug=slug, pr_id=pr_id, head_sha=head)
                actor = state.merged_by or "unknown"
                merged_at = state.merged_at or "unknown"
                signals.append(
                    ScanSignal(
                        kind="merge.unaudited",
                        summary=f"ALERT: {slug}#{pr_id} merged without an audit; confirm intent or revert",
                        payload={
                            "slug": slug,
                            "pr_id": pr_id,
                            "url": url,
                            "merged_sha": sha,
                            "merged_by": actor,
                            "merged_at": merged_at,
                            "independent_review_at_head": bool(review.standing_merge_safe),
                            "hold_at_head": bool(review.held_verdicts),
                            "overlay": self.overlay,
                        },
                    )
                )
        return signals
