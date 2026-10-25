import logging
from collections.abc import Callable, Iterable

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import MergeAudit
from teatree.core.notify import NotifyKind
from teatree.loop.scanners.base import ScanSignal
from teatree.messaging import notify_with_fallback

logger = logging.getLogger(__name__)

type MergeNotifier = Callable[..., object]


def record_unaudited_merges(signals: Iterable[ScanSignal], *, notify: MergeNotifier | None = None) -> None:
    send = notify or notify_with_fallback
    for signal in signals:
        if signal.kind != "merge.unaudited":
            continue
        payload = signal.payload
        slug = str(payload["slug"])
        pr_id = int(payload["pr_id"])
        sha = str(payload["merged_sha"])
        key = f"unaudited-merge:{slug}#{pr_id}:{sha[:12]}"
        try:
            if MergeAudit.objects.filter(merged_sha__iexact=sha).exists():
                continue
            independent_review = "yes" if payload["independent_review_at_head"] else "no"
            hold = "yes" if payload["hold_at_head"] else "no"
            text = (
                f"*ALERT: merge with no audit*\n"
                f"- PR: <{payload['url']}|{slug}#{pr_id}>\n"
                f"- Actor: {payload['merged_by']} · Merged: {payload['merged_at']}\n"
                f"- Independent review at head: {independent_review} · Hold at head: {hold}\n"
                f"- Merge SHA: {sha}\n"
                "- Next: Confirm this merge was intended; if not, revert it and review who can merge."
            )
            send(text, kind=NotifyKind.INFO, idempotency_key=key, audience=NotifyAudience.OWNER_ESCALATION)
        except Exception:
            logger.exception("unaudited merge alert failed for %s#%s", slug, pr_id)
