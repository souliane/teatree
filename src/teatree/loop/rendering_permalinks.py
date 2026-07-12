"""Build a ``mr_url → review-channel-post permalink`` map (#1113 enhancement).

When the ship pipeline posts an MR to the review channel it records a
:class:`ReviewRequestPost` row (``mr_url``, ``slack_channel_id``,
``slack_thread_ts``). The statusline reads that row and surfaces the
clickable post link alongside the MR ref so the operator can jump from
the statusline straight to the thread.

Split into its own module to mirror :mod:`teatree.loop.rendering_dms`
(one concern, tight contract: input ``actions`` → output
``url → permalink``). The permalink format matches the
``https://slack.com/archives/{channel}/p{ts.replace('.', '')}`` shape
the Slack web app accepts as a deep link and the rest of the codebase
already produces via :mod:`teatree.backends.slack`.
"""

from collections.abc import Iterable
from dataclasses import replace

from teatree.loop.dispatch import DispatchAction
from teatree.loop.rendering_classification import _ClassifiedActions


def _slack_permalink(channel_id: str, thread_ts: str) -> str:
    """Return ``https://slack.com/archives/{channel}/p{ts}`` or ``""``.

    Empty inputs collapse to ``""`` so a partial ``ReviewRequestPost``
    (channel id known, thread ts not yet captured) doesn't render a
    broken link.
    """
    if not channel_id or not thread_ts:
        return ""
    return f"https://slack.com/archives/{channel_id}/p{thread_ts.replace('.', '')}"


def _mr_urls_from_actions(actions: Iterable[DispatchAction]) -> set[str]:
    out: set[str] = set()
    for action in actions:
        if action.kind != "statusline":
            continue
        if action.zone not in {"action_needed", "in_flight"}:
            continue
        payload = action.payload if isinstance(action.payload, dict) else {}
        url = payload.get("url")
        if isinstance(url, str) and url:
            out.add(url)
    return out


def build_review_post_permalinks(actions: Iterable[DispatchAction]) -> dict[str, str]:
    """Return ``mr_url → Slack permalink`` for MRs posted to the review channel.

    Reads :class:`ReviewRequestPost` rows for every MR URL referenced by
    *actions* and rebuilds the canonical archive permalink from
    ``slack_channel_id`` + ``slack_thread_ts``. Empty result on Django not
    ready, on DB error, or when no row matches — the renderer treats
    missing entries as "no permalink to surface" and the MR ref renders
    normally without the extra link chunk.

    #1156 (Culprit A): filters out MRs whose :class:`PullRequest` row is
    in the MERGED state so the renderer never surfaces a permalink to a
    stale post that would 404 in Slack (the post may have been deleted
    or the thread archived after the MR merged).
    """
    urls = _mr_urls_from_actions(actions)
    if not urls:
        return {}
    try:
        from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

        model = apps.get_model("core", "ReviewRequestPost")
        pr_model = apps.get_model("core", "PullRequest")
    except Exception:  # noqa: BLE001 — a permalink-build failure degrades to no mapping
        return {}
    result: dict[str, str] = {}
    try:
        merged_urls = set(
            pr_model.objects.filter(url__in=urls, state="merged").values_list("url", flat=True),
        )
        rows = (
            model.objects.filter(mr_url__in=urls)
            .exclude(mr_url__in=merged_urls)
            .only("mr_url", "slack_channel_id", "slack_thread_ts")
        )
        for row in rows:
            permalink = _slack_permalink(row.slack_channel_id, row.slack_thread_ts)
            if permalink:
                result[row.mr_url] = permalink
    except Exception:  # noqa: BLE001 — a permalink-build failure degrades to no mapping
        return {}
    return result


def enrich_pr_refs_with_permalinks(c: _ClassifiedActions, permalinks: dict[str, str]) -> None:
    """Replace each ``_PRRef`` in *c* with a copy carrying its review permalink.

    The frozen ``_PRRef`` dataclass holds the permalink; the renderer
    surfaces it as an extra clickable chunk in
    :func:`_render_canonical_item`. No-op when *permalinks* is empty so
    the legacy ``action_prs``/``inflight_prs`` lists round-trip unchanged
    when no ``ReviewRequestPost`` rows exist.
    """
    if not permalinks:
        return
    for bucket in (c.action_prs, c.inflight_prs):
        for overlay_key, refs in list(bucket.items()):
            bucket[overlay_key] = [
                replace(r, review_permalink=permalinks[r.url]) if r.url in permalinks else r for r in refs
            ]
