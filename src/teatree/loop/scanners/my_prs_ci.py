"""Read an own-MR's CI verdict the list payload never carried, bounded and memoised.

A cross-project MR list (``/merge_requests?scope=created_by_me``) carries no
``head_pipeline``, no ``status_check_rollup`` and no ``mergeable_state``, so every
MR in it resolves to the empty status — which
:mod:`teatree.loop.scanners.my_prs` reads as legitimately-in-progress. The red-MR
lane is therefore structurally unreachable on that payload, however red the MR is.
This module fills that gap per MR.

The read routes through :meth:`CodeHostQuery.required_checks_status`, never
:func:`classify_required_rollup` directly: on GitLab ``fetch_required_checks_rollup``
returns PIPELINE entries rather than check-runs, and the classifier reads an empty
required set as ``"green"`` — so calling it directly would manufacture a green for
an MR whose CI nobody looked at. ``required_checks_status`` dispatches on the forge
kind and applies the GitLab pipeline verdict instead.

It is a merge-gate read, so it fails CLOSED: a rollup query that errors surfaces as
``"failed"`` rather than as unknown. Carried through deliberately — a false red
routes the MR to the debug agent, which is where an unreadable pipeline belongs;
the opposite bias would let a genuinely red MR read as benign.

Cost control is two-layered. The per-tick cap lives on the instance (the loop
builds one enricher per tick, so a fresh instance is a fresh budget) and the memo
is module-level, keyed on the head SHA: an MR that nobody pushed to is read once,
ever, and a new push is a new key.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)

#: Forge verdict → the pipeline-status vocabulary ``my_prs`` classifies on. A verdict
#: outside this table stays unknown rather than being coerced toward either pole.
_STATUS_BY_VERDICT: dict[str, str] = {
    "green": "success",
    "pending": "pending",
    "failed": "failed",
}

#: Per tick, per overlay. Small because the enrichment only has to reach the MRs the
#: operator actually has open — a backlog larger than this drains over a few ticks,
#: and the memo means it stays drained.
DEFAULT_MAX_PER_TICK = 6

#: The whole point of keying on the head SHA: an unchanged MR is never re-read. Bounded
#: so a long-lived loop process cannot grow it without limit.
_MEMO_CAPACITY = 512

_MEMO: dict[tuple[str, str], str] = {}


def reset_ci_memo() -> None:
    """Forget every memoised verdict — the test seam, and the only way to clear it."""
    _MEMO.clear()


def _required_checks_status(url: str) -> str:
    """The merge gate's own live-forge verdict for the MR at *url*."""
    from teatree.core.merge.ci_rollup import CodeHostQuery  # noqa: PLC0415 — deferred: tick-time, keeps import light

    ref = pr_ref_from_url(url)
    if ref is None:
        return ""
    return CodeHostQuery.for_ref(ref).required_checks_status()


@dataclass(slots=True)
class BoundedCiEnricher:
    """At most *max_per_tick* live CI reads, each memoised against its head SHA."""

    resolve: Callable[[str], str] = field(default=_required_checks_status)
    max_per_tick: int = DEFAULT_MAX_PER_TICK
    _spent: int = 0

    def status_for(self, *, url: str, head_sha: str) -> str:
        """The MR's pipeline status, or ``""`` when it was not read this tick.

        Both identifiers are required: without a URL there is nothing to query, and
        without a head SHA there is no key that a later push would invalidate — a
        verdict memoised against a moving target is worse than no verdict.
        """
        if not url or not head_sha:
            return ""
        key = (url, head_sha)
        memoised = _MEMO.get(key)
        if memoised is not None:
            return memoised
        if self._spent >= self.max_per_tick:
            return ""
        self._spent += 1
        status = _STATUS_BY_VERDICT.get(self._read(url), "")
        if status:
            self._memoise(key, status)
        return status

    def _read(self, url: str) -> str:
        try:
            return self.resolve(url)
        except Exception as exc:  # noqa: BLE001 — a forge read must never crash a tick.
            logger.warning("my_prs_ci: CI read failed for %s: %s", url, exc)
            return ""

    @staticmethod
    def _memoise(key: tuple[str, str], status: str) -> None:
        if len(_MEMO) >= _MEMO_CAPACITY:
            _MEMO.clear()
        _MEMO[key] = status


__all__ = ["DEFAULT_MAX_PER_TICK", "BoundedCiEnricher", "reset_ci_memo"]
