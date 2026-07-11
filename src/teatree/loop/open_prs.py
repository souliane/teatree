"""Open-PR snapshot cache + statusline anchor (#271).

The statusline shows how many PRs the user has open (drafts included) for
the teatree overlays. Re-querying the code host on every statusline render
would burn the API rate limit, so the snapshot is written ONCE per tick —
from the ``my_pr.*`` signals the loop's :class:`MyPrsScanner` already
collected — to a JSON sidecar next to the statusline file, and the anchor
formatter reads that cache at render time. No statusline render ever calls
the code host: the tick is the single fetch point, the sidecar is the cache.

This mirrors the ``tick-meta.json`` freshness sidecar (:mod:`tick_freshness`):
the loop captures expensive state during the tick and hands the rendered
snapshot to the cheap display path. The anchor lives here, beside the cache
it reads, rather than in :mod:`statusline` — locality of behaviour, and it
keeps the statusline module under its size budget.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypedDict

from teatree.loop.scanners.base import ScanSignal
from teatree.loop.statusline import StatuslineEntry, ZoneItem, default_path
from teatree.types import RawAPIDict

# Every ``my_pr.*`` signal the MyPrsScanner emits is an OPEN PR — the
# scanner only queries ``is:open`` PRs and partitions them by attention
# state (failed pipeline / unresolved notes / plain open). The snapshot
# unions all three so the count reflects every open PR, drafts included.
_OPEN_PR_SIGNAL_KINDS = frozenset({"my_pr.open", "my_pr.draft_notes", "my_pr.failed"})

CACHE_FILENAME = "open-prs.json"

# Cap the per-PR list so a busy account doesn't flood the anchor; the
# headline keeps the full count and an overflow row carries the remainder.
MAX_OPEN_PRS_LISTED = 5


class OpenPrRow(TypedDict):
    iid: int
    title: str
    url: str
    overlay: str
    draft: bool


@dataclass(frozen=True, slots=True)
class OpenPr:
    iid: int
    title: str
    url: str
    overlay: str
    draft: bool


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _is_draft(raw: RawAPIDict) -> bool:
    """Read the draft flag across host shapes.

    GitHub search-issues PRs and GitLab MRs both expose ``draft`` (bool);
    older GitLab payloads carry the legacy ``work_in_progress`` alias.
    """
    for key in ("draft", "work_in_progress"):
        value = raw.get(key)
        if isinstance(value, bool):
            return value
    return False


def open_prs_from_signals(signals: list[ScanSignal]) -> list[OpenPr]:
    """Project the loop's ``my_pr.*`` signals into open-PR snapshot rows.

    Reuses the data the tick already fetched — never queries the host —
    so building the snapshot costs zero extra API calls. Deduped by URL so
    a PR surfaced under two identities (the scanner's multi-alias union)
    counts once.
    """
    seen: set[str] = set()
    prs: list[OpenPr] = []
    for signal in signals:
        if signal.kind not in _OPEN_PR_SIGNAL_KINDS:
            continue
        payload = signal.payload
        url = _as_str(payload.get("url"))
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        raw = payload.get("raw")
        raw_dict: RawAPIDict = raw if isinstance(raw, dict) else {}
        prs.append(
            OpenPr(
                iid=_as_int(payload.get("iid")),
                title=_as_str(payload.get("title")),
                url=url,
                overlay=_as_str(payload.get("overlay")),
                draft=_is_draft(raw_dict),
            )
        )
    return prs


def cache_path(statusline_path: Path) -> Path:
    return statusline_path.with_name(CACHE_FILENAME)


def write_open_prs_cache(prs: list[OpenPr], *, statusline_path: Path) -> Path:
    """Atomically write the open-PR snapshot beside the statusline file.

    Mirrors :func:`teatree.loop.tick_freshness._write_tick_meta`: the parent
    dir is ensured (the skip path writes the sidecar without the ``render``
    that would create it) so an observability write can never crash the tick.
    """
    target = cache_path(statusline_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([asdict(pr) for pr in prs]) + "\n", encoding="utf-8")
    return target


def read_open_prs_cache(*, statusline_path: Path) -> list[OpenPr]:
    """Read the cached open-PR snapshot, or ``[]`` when absent/corrupt.

    Fails open to ``[]`` on a missing, empty, or malformed sidecar so a
    bad cache can never blank the statusline — the anchor simply omits the
    open-PR line until the next tick rewrites a good snapshot.
    """
    target = cache_path(statusline_path)
    try:
        body = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return []
    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not isinstance(rows, list):
        return []
    return [_row_to_open_pr(row) for row in rows if isinstance(row, dict)]


def _row_to_open_pr(row: OpenPrRow) -> OpenPr:
    return OpenPr(
        iid=_as_int(row.get("iid")),
        title=_as_str(row.get("title")),
        url=_as_str(row.get("url")),
        overlay=_as_str(row.get("overlay")),
        draft=bool(row.get("draft")),
    )


def open_prs_anchor(*, target: Path | None = None, colorize: bool = False) -> list[ZoneItem]:
    """Return the open-PR summary anchor from the cached tick snapshot (#271).

    Reads the snapshot the tick wrote to the ``open-prs.json`` sidecar
    (:func:`read_open_prs_cache`) — never the code host — so the line costs
    zero API calls and stays under the rate limit. The snapshot already
    covers every open PR (drafts included) the loop's
    :class:`~teatree.loop.scanners.my_prs.MyPrsScanner` surfaced for the
    configured teatree overlays.

    Shape: a headline count (``open PRs: 3 (1 draft)``) followed by one
    clickable ``  #<iid> <title>`` row per PR — draft rows prefixed
    ``[draft]`` — capped at :data:`MAX_OPEN_PRS_LISTED` with a trailing
    ``(+N more)`` overflow row.

    Returns ``[]`` when no PR is open so a quiet machine shows no line.
    *colorize* is accepted for call-site symmetry with the other anchors;
    the open-PR rows carry no per-line color of their own (they ride the
    anchor zone's dim). Degrades to ``[]`` on any read error so a broken
    cache can never blank the statusline.
    """
    _ = colorize
    try:
        prs = _read_open_prs(target or default_path())
    except Exception:  # noqa: BLE001 — a fetch failure degrades to no PRs, never breaks the tick
        return []
    if not prs:
        return []

    draft_count = sum(1 for pr in prs if pr.draft)
    headline = f"open PRs: {len(prs)}"
    if draft_count:
        headline += f" ({draft_count} draft)"

    rows: list[ZoneItem] = [headline, *(_open_pr_row(pr) for pr in prs[:MAX_OPEN_PRS_LISTED])]
    overflow = len(prs) - MAX_OPEN_PRS_LISTED
    if overflow > 0:
        rows.append(f"  (+{overflow} more)")
    return rows


def _read_open_prs(statusline_path: Path) -> list[OpenPr]:
    """Thin seam over :func:`read_open_prs_cache` (tests stub this directly)."""
    return read_open_prs_cache(statusline_path=statusline_path)


def _open_pr_row(pr: OpenPr) -> ZoneItem:
    marker = "[draft] " if pr.draft else ""
    text = f"  #{pr.iid} {marker}{pr.title}".rstrip()
    return StatuslineEntry(text=text, url=pr.url) if pr.url else text


__all__ = [
    "CACHE_FILENAME",
    "MAX_OPEN_PRS_LISTED",
    "OpenPr",
    "OpenPrRow",
    "cache_path",
    "open_prs_anchor",
    "open_prs_from_signals",
    "read_open_prs_cache",
    "write_open_prs_cache",
]
