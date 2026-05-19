"""Tabular per-tick dashboard — Slack-DM-routed (#1005).

The statusline gives a compact one-glance view but scrolls off the chat
too fast — the user wants a tabular dashboard delivered to their Slack
DM after every meaningful tick, so a phone glance is enough to follow
what the loop is doing.

Two halves:

* :func:`record_actions` appends one JSON line per dispatched action to
    the ``tick-actions.jsonl`` sidecar at ``$T3_DATA_DIR/tick-actions.jsonl``
    every time :func:`teatree.loop.tick.run_tick` finalises a tick.  The
    file rotates at :data:`_ROTATE_LIMIT` lines so it stays bounded.
* :func:`render_dashboard` reads the sidecar and renders a markdown table
    grouped by overlay, one row per active item, with linked refs (Slack
    ``<url|label>`` mrkdwn or markdown ``[label](url)`` depending on
    ``fmt``).  Identity-dedup filters reassignments between aliases of the
    same human; the cross-overlay bleed gate filters items not owned by
    the dispatching overlay's ``get_repos()``.

The Slack send is idempotent per content + 5-min-bucketed tick timestamp
so a re-run of the same tick never spams.
"""

import datetime as dt
import enum
import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from teatree.paths import DATA_DIR

if TYPE_CHECKING:
    from teatree.loop.dispatch import DispatchAction

# Shape of `teatree.notify.notify_user` — exposed here so tests can pass a
# simple stand-in without importing the real helper (which would pull
# Django into a pure-logic test path).
type NotifyUserFn = Callable[..., bool]

logger = logging.getLogger(__name__)

_ROTATE_LIMIT = 1000

_PR_URL_RE = re.compile(r"https?://[^\s>|]+/(?:merge_requests|pull|pulls)/(\d+)")
_ISSUE_URL_RE = re.compile(r"https?://[^\s>|]+/issues/(\d+)")


class DashboardFormat(enum.StrEnum):
    """Output format selector — markdown for stdout, slack mrkdwn for DM."""

    MARKDOWN = "markdown"
    SLACK = "slack"


def default_actions_path() -> Path:
    """Canonical path of the per-tick actions sidecar."""
    return DATA_DIR / "tick-actions.jsonl"


@dataclass(frozen=True, slots=True)
class TickAction:
    """One row in ``tick-actions.jsonl``.

    Fields are the minimum to render the dashboard without re-reading the
    Django ORM later — every cell of the table comes from this record.
    """

    ts: str
    scanner: str
    overlay: str
    action_kind: str
    ref: str
    label: str
    url: str
    before_state: str = ""
    after_state: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TickAction":
        return cls(
            ts=str(raw.get("ts", "")),
            scanner=str(raw.get("scanner", "")),
            overlay=str(raw.get("overlay", "")),
            action_kind=str(raw.get("action_kind", "")),
            ref=str(raw.get("ref", "")),
            label=str(raw.get("label", "")),
            url=str(raw.get("url", "")),
            before_state=str(raw.get("before_state", "")),
            after_state=str(raw.get("after_state", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "ts": self.ts,
            "scanner": self.scanner,
            "overlay": self.overlay,
            "action_kind": self.action_kind,
            "ref": self.ref,
            "label": self.label,
            "url": self.url,
            "before_state": self.before_state,
            "after_state": self.after_state,
        }


# ── recording (called from tick) ─────────────────────────────────────


def _payload_overlay(action: "DispatchAction") -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    value = payload.get("overlay")
    return value if isinstance(value, str) else ""


def _payload_url(action: "DispatchAction") -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    for key in ("url", "issue_url"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _ref_for(action: "DispatchAction") -> str:
    """Derive a short ref token (``#42``, ``!57``, ``slack``) for the row."""
    payload = action.payload if isinstance(action.payload, dict) else {}
    iid = payload.get("iid")
    url = _payload_url(action)
    if isinstance(iid, int) and iid > 0:
        prefix = "!" if _PR_URL_RE.search(url) else "#"
        return f"{prefix}{iid}"
    pr_match = _PR_URL_RE.search(url)
    if pr_match:
        return f"!{pr_match.group(1)}"
    issue_match = _ISSUE_URL_RE.search(url)
    if issue_match:
        return f"#{issue_match.group(1)}"
    ticket = payload.get("ticket_number")
    if isinstance(ticket, str) and ticket:
        return f"#{ticket}"
    return ""


def _label_for(action: "DispatchAction") -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return action.detail.strip()


def _state_pair(action: "DispatchAction") -> tuple[str, str]:
    payload = action.payload if isinstance(action.payload, dict) else {}
    before = payload.get("before_state") or payload.get("state") or ""
    after = payload.get("after_state") or ""
    return (str(before), str(after))


def _is_identity_self_handoff(action: "DispatchAction", identities: frozenset[str]) -> bool:
    """Suppress reassignments between two of the operator's own identities.

    Mirrors :class:`teatree.loop.scanners.ticket_dispositions` identity
    dedup: when both the old owner and every new owner are in the user's
    own alias set, the move is plumbing noise — not a meaningful action
    to surface on the dashboard.
    """
    payload = action.payload if isinstance(action.payload, dict) else {}
    if payload.get("reason") != "unassigned":
        return False
    old = payload.get("old_owner")
    new_owners = payload.get("new_owners")
    if not isinstance(old, str) or not isinstance(new_owners, list):
        return False
    if not identities:
        return False
    if old not in identities:
        return False
    owners = [o for o in new_owners if isinstance(o, str)]
    if not owners:
        return False
    return all(o in identities for o in owners)


def _belongs_to_overlay(action: "DispatchAction", overlay: str, overlay_repos: dict[str, frozenset[str]]) -> bool:
    """Cross-overlay bleed gate — only keep rows owned by ``overlay`` repos.

    When an overlay's scan tag does not match the URL's owning repo (see
    persistence ``_owning_overlay``), the action would otherwise leak into
    the dashboard under the wrong overlay header. We drop the row if the
    URL's repo is not in the dispatching overlay's ``get_repos()``.

    Permissive default: rows with no URL, no overlay tag, or no
    registered overlay-repos map keep their slot — the gate fires only
    when there is concrete evidence the row is foreign.
    """
    if not overlay or overlay not in overlay_repos:
        return True
    url = _payload_url(action)
    if not url:
        return True
    repos = overlay_repos.get(overlay, frozenset())
    if not repos:
        return True
    return any(repo in url for repo in repos)


def record_actions(
    actions: "Iterable[DispatchAction]",
    *,
    now: dt.datetime,
    path: Path | None = None,
    identities: Iterable[str] = (),
    overlay_repos: dict[str, frozenset[str]] | None = None,
) -> int:
    """Append ``tick-actions.jsonl`` rows for ``actions`` and return the count.

    Identity-dedup and cross-overlay bleed are applied here so the sidecar
    is already clean by the time :func:`render_dashboard` reads it — the
    renderer is a pure formatter.

    Rotates the file when it would exceed :data:`_ROTATE_LIMIT` lines:
    drops the oldest half before appending.
    """
    target = path or default_actions_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    identity_set = frozenset(i for i in identities if i)
    repos_map = overlay_repos or {}
    rows: list[TickAction] = []
    ts = now.replace(microsecond=0).isoformat()
    for action in actions:
        if action.kind not in {"statusline", "agent", "mechanical", "webhook"}:
            continue
        if _is_identity_self_handoff(action, identity_set):
            continue
        overlay = _payload_overlay(action)
        if not _belongs_to_overlay(action, overlay, repos_map):
            continue
        before, after = _state_pair(action)
        rows.append(
            TickAction(
                ts=ts,
                scanner=str((action.payload or {}).get("scanner", "")),
                overlay=overlay,
                action_kind=action.kind,
                ref=_ref_for(action),
                label=_label_for(action),
                url=_payload_url(action),
                before_state=before,
                after_state=after,
            ),
        )
    if not rows:
        return 0
    _rotate_if_needed(target)
    with target.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
    return len(rows)


def _rotate_if_needed(path: Path) -> None:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    if len(lines) < _ROTATE_LIMIT:
        return
    keep = lines[_ROTATE_LIMIT // 2 :]
    path.write_text("".join(keep), encoding="utf-8")


# ── reading / rendering ──────────────────────────────────────────────


def _load_actions(path: Path) -> list[TickAction]:
    if not path.is_file():
        return []
    out: list[TickAction] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("tick-actions.jsonl: skipping malformed line: %s", line[:120])
            continue
        if not isinstance(data, dict):
            continue
        out.append(TickAction.from_dict(data))
    return out


def _link(label: str, url: str, *, fmt: DashboardFormat) -> str:
    if not url:
        return label
    if fmt is DashboardFormat.SLACK:
        # Slack mrkdwn: <url|label>; escape pipes inside the label.
        clean = label.replace("|", "\\|")
        return f"<{url}|{clean}>"
    # Plain markdown
    return f"[{label}]({url})"


def _state_cell(row: TickAction) -> str:
    if row.before_state and row.after_state:
        return f"{row.before_state} → {row.after_state}"
    if row.after_state:
        return row.after_state
    return row.before_state


def _last_action_cell(row: TickAction) -> str:
    parts = [p for p in (row.scanner, row.action_kind) if p]
    return " · ".join(parts) if parts else row.action_kind


@dataclass(slots=True)
class _GroupedRows:
    """One overlay's deduped row set, preserving insertion order."""

    overlay: str
    rows: list[TickAction] = field(default_factory=list)


def _group_by_overlay(actions: list[TickAction]) -> list[_GroupedRows]:
    """Group by overlay, deduping rows sharing the same (overlay, ref, url).

    Two scanners surfacing the same observation should collapse to one
    table row — the renderer is the dedup boundary.
    """
    buckets: dict[str, _GroupedRows] = {}
    order: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for row in actions:
        key = (row.overlay, row.ref, row.url)
        if key in seen:
            continue
        seen.add(key)
        if row.overlay not in buckets:
            buckets[row.overlay] = _GroupedRows(overlay=row.overlay)
            order.append(row.overlay)
        buckets[row.overlay].rows.append(row)
    return [buckets[name] for name in order]


def render_dashboard(
    *,
    fmt: DashboardFormat = DashboardFormat.MARKDOWN,
    source_path: Path | None = None,
    self_dm_marker: bool = False,
) -> str:
    """Render the dashboard from ``source_path`` (default sidecar).

    ``self_dm_marker=True`` appends ``(this DM)`` to the row that
    represents the dashboard send itself — matches the manually-produced
    dashboard's self-reference so the user can scan-confirm the DM they
    just opened is the same row in the table.
    """
    path = source_path or default_actions_path()
    actions = _load_actions(path)
    groups = _group_by_overlay(actions)
    if not groups:
        return "_No tick actions recorded yet._\n"

    lines: list[str] = ["# Loop dashboard", ""]
    for group in groups:
        header = f"[{group.overlay}]" if group.overlay else "[(unscoped)]"
        lines.extend(
            [
                f"## {header}",
                "",
                "| Ref | Title | State | Last action | URL |",
                "| --- | --- | --- | --- | --- |",
            ],
        )
        for row in group.rows:
            ref_cell = _link(row.ref, row.url, fmt=fmt) if row.ref else "—"
            title_cell = row.label or "—"
            if self_dm_marker and row.action_kind == "slack_dm":
                title_cell = f"{title_cell} (this DM)"
            state_cell = _state_cell(row) or "—"
            action_cell = _last_action_cell(row)
            url_cell = _link("link", row.url, fmt=fmt) if row.url else "—"
            lines.append(
                f"| {ref_cell} | {title_cell} | {state_cell} | {action_cell} | {url_cell} |",
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── Slack send (idempotent) ──────────────────────────────────────────


def _bucket_key(tick_ts: dt.datetime, *, bucket_minutes: int = 5) -> str:
    """Round ``tick_ts`` down to the nearest ``bucket_minutes`` for idempotency."""
    epoch = int(tick_ts.timestamp())
    bucket = bucket_minutes * 60
    return str((epoch // bucket) * bucket)


def content_hash(rendered: str) -> str:
    """Stable short SHA used in the idempotency key."""
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:12]


def idempotency_key_for(rendered: str, *, tick_ts: dt.datetime) -> str:
    """Build the ``notify_user`` idempotency key for ``rendered`` at ``tick_ts``."""
    return f"dashboard-{content_hash(rendered)}-{_bucket_key(tick_ts)}"


def send_dashboard(
    rendered: str,
    *,
    tick_ts: dt.datetime,
    notify_user_fn: NotifyUserFn,
) -> bool:
    """Send ``rendered`` via ``notify_user_fn`` with a deterministic idempotency key.

    The caller injects the notifier (typically
    :func:`teatree.notify.notify_user`) — keeping the loop module free of
    a top-level ``teatree.notify`` dependency. Returns whatever the
    notifier returns: ``True`` when the send happened (or was an
    idempotent repeat), ``False`` when the bot is unconfigured or the
    send failed.
    """
    key = idempotency_key_for(rendered, tick_ts=tick_ts)
    return bool(
        notify_user_fn(
            rendered,
            kind="info",
            idempotency_key=key,
        ),
    )


__all__ = [
    "DashboardFormat",
    "TickAction",
    "content_hash",
    "default_actions_path",
    "idempotency_key_for",
    "record_actions",
    "render_dashboard",
    "send_dashboard",
]
