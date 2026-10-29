"""Bounded convergence of live pull request ledger rows."""

import time
from collections.abc import Callable

from teatree.core.models import PullRequest


def settle_live_pr_rows(*, overlay: str, read_state: Callable[[str], str], budget: int) -> int:
    if budget <= 0:
        return 0
    live = PullRequest.objects.live()
    if overlay:
        live = live.filter(overlay=overlay)
    pages = max(1, (live.count() + budget - 1) // budget)
    offset = (int(time.time() // 3600) % pages) * budget
    ids = list(live.order_by("pk").values_list("pk", flat=True)[offset : offset + budget])
    return PullRequest.objects.filter(pk__in=ids).reconcile_forge_states(read_state=read_state)
