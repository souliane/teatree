"""Render followup.json data into an HTML dashboard.

Pure functions — no I/O, no side effects. Takes a dict (parsed JSON),
returns an HTML string.
"""

import re
from datetime import UTC, datetime
from html import escape

# ---------------------------------------------------------------------------
# CSS (Tokyo Night palette)
# ---------------------------------------------------------------------------

CSS = """\
:root { --bg: #1a1b26; --card: #24283b; --text: #c0caf5; --muted: #565f89; \
--green: #9ece6a; --red: #f7768e; --yellow: #e0af68; --blue: #7aa2f7; \
--border: #3b4261; --purple: #bb9af7; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'SF Mono', 'Fira Code', \
monospace; background: var(--bg); color: var(--text); padding: 1.5rem; \
max-width: 1400px; margin: 0 auto; }
h1 { font-size: 1.3rem; margin-bottom: 0.4rem; }
.meta { color: var(--muted); font-size: 0.78rem; margin-bottom: 1.2rem; }
h2 { font-size: 1.05rem; margin: 1.2rem 0 0.4rem; color: var(--blue); }
table { width: 100%; border-collapse: collapse; margin-bottom: 0.8rem; }
th { text-align: left; padding: 0.35rem 0.5rem; border-bottom: 2px solid \
var(--border); color: var(--muted); font-size: 0.7rem; text-transform: uppercase; \
letter-spacing: 0.05em; }
td { padding: 0.35rem 0.5rem; border-bottom: 1px solid var(--border); \
font-size: 0.82rem; vertical-align: top; }
td.ticket-cell { border-bottom: 2px solid var(--border); }
a { color: var(--blue); text-decoration: none; }
a:hover { text-decoration: underline; }
.pill { display: inline-block; padding: 0.12rem 0.4rem; border-radius: 4px; \
font-size: 0.72rem; font-weight: 600; white-space: nowrap; }
a.pill, .pill a { text-decoration: none; }
a.pill:hover, .pill a:hover { text-decoration: none; filter: brightness(1.3); }
.success { background: rgba(158,206,106,0.15); color: var(--green); }
.failed { background: rgba(247,118,142,0.15); color: var(--red); }
.running { background: rgba(224,175,104,0.15); color: var(--yellow); }
.pending { background: rgba(224,175,104,0.1); color: var(--muted); }
.skipped { background: rgba(86,95,137,0.2); color: var(--muted); }
.approved { background: rgba(187,154,247,0.15); color: var(--purple); }
.section { background: var(--card); border-radius: 8px; padding: 0.8rem 1rem; \
margin-bottom: 0.8rem; }
code { background: rgba(122,162,247,0.1); padding: 0.1rem 0.3rem; \
border-radius: 3px; font-size: 0.78rem; }
.ticket-title { font-weight: 500; font-size: 0.82rem; }
.status-stack { display: flex; flex-direction: column; gap: 0.2rem; }"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PIPELINE_CSS = {
    "success": ("success", "&#x2705; success"),
    "failed": ("failed", "&#x274C; failed"),
    "running": ("running", "&#x1F504; running"),
    "pending": ("pending", "&#x23F3; pending"),
}

_STATUS_CSS = {
    "doing": "running",
    "in progress": "success",
    "in progress (dev/config)": "success",
    "technical review": "success",
    "tech review": "success",
    "dev review": "success",
}

_STATUS_ICONS = {
    "doing": "&#x1F527;",
    "in progress": "&#x2705;",
    "in progress (dev/config)": "&#x2705;",
    "technical review": "&#x1F440;",
    "tech review": "&#x1F440;",
    "dev review": "&#x2705;",
}

_REVIEW_COMMENT_CSS = {
    "waiting_reviewer": ("pending", "&#x23F3; Waiting reviewer"),
    "addressed": ("success", "&#x2705; Addressed"),
    "needs_reply": ("running", "&#x270F;&#xFE0F; Needs reply"),
}


def _e(text: str) -> str:
    return escape(str(text))


def _pill(css_class: str, content: str, *, link: str = "") -> str:
    if link:
        return f'<a href="{_e(link)}" class="pill {css_class}">{content}</a>'
    return f'<span class="pill {css_class}">{content}</span>'


def _pipeline_pill(status: str | None, url: str | None, skip_reason: str = "") -> str:
    if skip_reason:
        label = f"&#x274C; {_e(skip_reason)}" if status == "failed" else "&mdash;"
        css = "failed" if status == "failed" else "skipped"
        return _pill(css, label, link=url or "")
    if not status:
        return "&mdash;"
    css, label = _PIPELINE_CSS.get(status, ("pending", f"&#x23F3; {_e(status)}"))
    return _pill(css, label, link=url or "")


def _status_pill(status_text: str, ticket_url: str = "") -> str:
    key = status_text.lower().replace("process::", "").strip()
    css = _STATUS_CSS.get(key, "running")
    icon = _STATUS_ICONS.get(key, "&#x1F527;")
    label = status_text.replace("Process::", "").strip()
    return _pill(css, f"{icon} {_e(label)}", link=ticket_url)


def _extract_feature_flag(ticket: dict, mrs_data: dict, mr_keys: list[str]) -> str:
    flag = ticket.get("feature_flag")
    if flag:
        return flag
    # Try extracting from MR titles: [flag_name] suffix
    for mk in mr_keys:
        mr = mrs_data.get(mk, {})
        title = mr.get("title", "")
        m = re.search(r"\[(\w+)]$", title.strip())
        if m and m.group(1).lower() != "none":
            return m.group(1)
    return ""


_MINUTES_PER_HOUR = 60


def _format_time_ago(generated_at: str) -> str:
    try:
        gen = datetime.fromisoformat(generated_at)
    except (ValueError, AttributeError):
        return ""
    now = datetime.now(UTC)
    diff_minutes = int((now - gen).total_seconds() / _MINUTES_PER_HOUR)
    if diff_minutes < 1:
        return "just now"
    if diff_minutes < _MINUTES_PER_HOUR:
        return f"{diff_minutes}m ago"
    hours = diff_minutes // _MINUTES_PER_HOUR
    minutes = diff_minutes % _MINUTES_PER_HOUR
    if minutes:
        return f"{hours}h {minutes}m ago"
    return f"{hours}h ago"


def _approval_pill(mr: dict) -> str:
    approvals = mr.get("approvals")
    if approvals:
        count = approvals.get("count", 0)
        required = approvals.get("required", 1)
    else:
        count = 0
        required = 1
    if count >= required > 0:
        return _pill("approved", f"&#x2705; {count}/{required}")
    return _pill("pending", f"{count}/{required}")


def _review_request_cell(mr: dict) -> str:
    if mr.get("skipped"):
        return _pill("skipped", "&#x23ED;&#xFE0F; skipped")
    if not mr.get("review_requested"):
        return _pill("pending", "&#x23F3; not sent")
    channel = mr.get("review_channel", "")
    permalink = mr.get("review_permalink", "")
    if permalink:
        inner = f'<a href="{_e(permalink)}" style="color:var(--green)">{_e(channel)}</a>'
        return f'<span class="pill success">&#x2705; {inner}</span>'
    # Requested but no permalink — waiting for something
    pipeline = mr.get("pipeline_status", "")
    if pipeline in {"running", "pending"}:
        return _pill("pending", "&#x23F3; waiting pipeline")
    return _pill("pending", "&#x23F3; waiting group")


def _e2e_cell(mr: dict) -> str:
    url = mr.get("e2e_test_plan_url")
    if url:
        return _pill("success", "&#x2705; test plan", link=url)
    return "&mdash;"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

_WORK_TABLE_HEADERS = (
    "<tr><th>Ticket</th><th>Status</th><th>Feature Flag</th>"
    "<th>MR</th><th>Pipeline</th><th>E2E</th>"
    "<th>Review Request</th><th>Approved</th></tr>\n"
)


def _render_ticket_cells(ticket_id: str, ticket: dict, mr_count: int, flag: str) -> str:
    rowspan = f' rowspan="{mr_count}"' if mr_count > 1 else ""
    url = ticket.get("url", "")

    # Ticket cell
    if url:
        ticket_label = f'<a href="{_e(url)}">#{_e(ticket_id)}</a>'
    elif ticket_id.startswith("misc"):
        ticket_label = "misc"
    else:
        ticket_label = f"#{_e(ticket_id)}"
    title = _e(ticket.get("title", ""))
    ticket_cell = (
        f'<td class="ticket-cell"{rowspan}>\n'
        f"    {ticket_label}<br>\n"
        f'    <span class="ticket-title">{title}</span>\n'
        f"  </td>"
    )

    # Status cell — collect all status fields
    statuses = _collect_statuses(ticket)
    if not statuses:
        status_inner = _pill("skipped", "&mdash;")
    elif len(statuses) == 1:
        status_inner = _status_pill(statuses[0], url)
    else:
        pills = "\n      ".join(_status_pill(s, url) for s in statuses)
        status_inner = f'<div class="status-stack">\n      {pills}\n    </div>'
    status_cell = f'<td class="ticket-cell"{rowspan}>\n    {status_inner}\n  </td>'

    # Feature flag cell
    if flag:
        flag_cell = f'<td class="ticket-cell"{rowspan}><code>{_e(flag)}</code></td>'
    else:
        flag_cell = f'<td class="ticket-cell"{rowspan}>&mdash;</td>'

    return f"  {ticket_cell}\n  {status_cell}\n  {flag_cell}"


def _collect_statuses(ticket: dict) -> list[str]:
    statuses = []
    # Core field
    ts = ticket.get("tracker_status")
    if ts:
        statuses.append(ts)
    # Overlay fields (project-specific, e.g. platform status, external tracker status)
    gs = ticket.get("gitlab_status")
    if gs and gs not in statuses:
        statuses.append(gs)
    ns = ticket.get("notion_status")
    if ns and ns not in statuses:
        statuses.append(ns)
    return statuses


def _render_mr_row(mr: dict, mr_key: str, *, include_ticket_cells: str = "") -> str:
    repo = mr.get("repo", "")
    iid = mr_key.rsplit("!", maxsplit=1)[-1] if "!" in mr_key else mr_key
    mr_url = mr.get("url", "")
    mr_label = f"{repo} !{iid}" if repo else mr_key
    mr_cell = f'<a href="{_e(mr_url)}">{_e(mr_label)}</a>' if mr_url else _e(mr_label)

    pipeline_url = mr.get("pipeline_url") or ""
    if not pipeline_url and mr_url:
        pipeline_url = f"{mr_url}/pipelines"
    skip_reason = mr.get("skip_reason", "")
    pipeline = _pipeline_pill(mr.get("pipeline_status"), pipeline_url, skip_reason)

    e2e = _e2e_cell(mr)
    review = _review_request_cell(mr)

    approved = _pill("skipped", "&mdash;") if mr.get("skipped") else _approval_pill(mr)

    cells = f"  <td>{mr_cell}</td>\n"
    cells += f"  <td>{pipeline}</td>\n"
    cells += f"  <td>{e2e}</td>\n"
    cells += f"  <td>{review}</td>\n"
    cells += f"  <td>{approved}</td>"

    if include_ticket_cells:
        return f"<tr>\n{include_ticket_cells}\n{cells}\n</tr>"
    return f"<tr>\n{cells}\n</tr>"


def _render_inflight_section(data: dict) -> str:
    tickets = data.get("tickets", {})
    mrs_data = data.get("mrs", {})
    if not tickets:
        return ""

    rows: list[str] = []
    for ticket_id, ticket in tickets.items():
        mr_keys = ticket.get("mrs", [])
        active_keys = [k for k in mr_keys if k in mrs_data]
        if not active_keys:
            continue

        flag = _extract_feature_flag(ticket, mrs_data, active_keys)
        ticket_cells = _render_ticket_cells(ticket_id, ticket, len(active_keys), flag)

        for i, mk in enumerate(active_keys):
            mr = mrs_data[mk]
            if i == 0:
                rows.append(_render_mr_row(mr, mk, include_ticket_cells=ticket_cells))
            else:
                rows.append(_render_mr_row(mr, mk))

    if not rows:
        return ""

    body = "\n".join(rows)
    return (
        '<div class="section">\n'
        "<h2>&#x1F4CB; In-Flight Work</h2>\n"
        "<table>\n"
        f"{_WORK_TABLE_HEADERS}"
        f"{body}\n"
        "</table>\n"
        "</div>"
    )


def _render_review_comments_section(data: dict) -> str:
    tracking = data.get("review_comments_tracking", {})
    # Also include in-flight MRs with review_comments
    mrs_data = data.get("mrs", {})
    entries: dict[str, dict] = dict(tracking.items())

    for mk, mr in mrs_data.items():
        rc = mr.get("review_comments")
        if rc and mk not in entries:
            status_map = {"addressed": "addressed", "pending": "needs_reply"}
            entries[mk] = {
                "url": mr.get("url", ""),
                "status": status_map.get(rc.get("status", ""), rc.get("status", "")),
                "details": rc.get("details", ""),
            }

    if not entries:
        return ""

    rows: list[str] = []
    for mk, info in entries.items():
        url = info.get("url", "")
        repo_iid = mk.replace("!", " !")
        mr_link = f'<a href="{_e(url)}">{_e(repo_iid)}</a>' if url else _e(repo_iid)
        status = info.get("status", "")
        css, label = _REVIEW_COMMENT_CSS.get(status, ("pending", _e(status)))
        details = _e(info.get("details", ""))
        rows.append(f"<tr>\n  <td>{mr_link}</td>\n  <td>{_pill(css, label)}</td>\n  <td>{details}</td>\n</tr>")

    body = "\n".join(rows)
    return (
        '<div class="section">\n'
        "<h2>&#x1F4DD; Review Comments</h2>\n"
        "<table>\n"
        "<tr><th>MR</th><th>Status</th><th>Details</th></tr>\n"
        f"{body}\n"
        "</table>\n"
        "</div>"
    )


def _render_draft_mrs_section(data: dict) -> str:
    drafts = data.get("draft_mrs", {})
    if not drafts:
        return ""

    rows: list[str] = []
    for mk, draft in drafts.items():
        title = _e(draft.get("title", ""))
        repo = draft.get("repo", "")
        iid = mk.split("!")[-1] if "!" in mk else mk
        mr_url = draft.get("url", "")
        mr_label = f"{repo} !{iid}" if repo else mk
        mr_cell = f'<a href="{_e(mr_url)}">{_e(mr_label)}</a>' if mr_url else _e(mr_label)

        pipeline_url = draft.get("pipeline_url") or ""
        if not pipeline_url and mr_url:
            pipeline_url = f"{mr_url}/pipelines"
        pipeline = _pipeline_pill(draft.get("pipeline_status"), pipeline_url)

        ticket_cells = (
            f'  <td class="ticket-cell">&mdash;<br>'
            f'<span class="ticket-title">{title}</span></td>\n'
            f'  <td class="ticket-cell">{_pill("skipped", "&mdash;")}</td>\n'
            f'  <td class="ticket-cell">&mdash;</td>'
        )

        mr_cells = (
            f"  <td>{mr_cell}</td>\n  <td>{pipeline}</td>\n  <td>&mdash;</td>\n  <td>&mdash;</td>\n  <td>&mdash;</td>"
        )

        rows.append(f"<tr>\n{ticket_cells}\n{mr_cells}\n</tr>")

    body = "\n".join(rows)
    return (
        f'<div class="section">\n<h2>&#x1F4DD; Draft MRs</h2>\n<table>\n{_WORK_TABLE_HEADERS}{body}\n</table>\n</div>'
    )


def _render_actions_section(data: dict) -> str:
    actions = data.get("actions_log", [])
    if not actions:
        return ""

    items = "\n".join(f"<li>&#x2705; {_e(a)}</li>" for a in actions)
    return (
        '<div class="section">\n'
        "<h2>&#x1F527; Actions Taken This Session</h2>\n"
        '<ul style="list-style: none; font-size: 0.82rem; line-height: 1.7;">\n'
        f"{items}\n"
        "</ul>\n"
        "</div>"
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def render_dashboard(data: dict) -> str:
    generated_at = data.get("generated_at", "")
    time_ago = _format_time_ago(generated_at) if generated_at else ""

    # Format display date
    try:
        gen_dt = datetime.fromisoformat(generated_at)
        display_date = gen_dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        display_date = generated_at or "unknown"

    meta_text = f"Generated: {display_date} ({time_ago})" if time_ago else f"Generated: {display_date}"

    sections = [
        _render_inflight_section(data),
        _render_review_comments_section(data),
        _render_draft_mrs_section(data),
        _render_actions_section(data),
    ]
    body = "\n\n".join(s for s in sections if s)

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta http-equiv="refresh" content="120">\n'
        "<title>t3-followup Dashboard</title>\n"
        f"<style>\n  {CSS}\n</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>&#x1F680; t3-followup Dashboard</h1>\n"
        f'<p class="meta">{meta_text}</p>\n'
        "\n"
        f"{body}\n"
        "\n"
        "</body>\n"
        "</html>\n"
    )
