"""Merge one run onto the persisted state, then render the note body.

The rendering half of the one-note-per-ticket test-plan model (teatree #272):
:func:`merge_state` overlays a run's side(s) on the prior
:class:`~teatree.core.management.commands._test_plan.state.TestPlanState`
(freezing the side the run does not carry) and :func:`render_body` is a pure
function of the merged state. The state schema lives in :mod:`.state` and the
manifest parse in :mod:`.manifest`; this module re-exports both surfaces so
callers keep importing the whole test-plan string layer from one module.
"""

import json
from urllib.parse import urlparse

from teatree.core.management.commands._test_plan.manifest import (
    SideManifest,
    TestPlanManifest,
    WorkflowArtifacts,
    parse_manifest,
    validate_template,
)
from teatree.core.management.commands._test_plan.scenario import render_scenario_plan
from teatree.core.management.commands._test_plan.state import (
    DEFAULT_TEMPLATE,
    KNOWN_TEMPLATES,
    SideState,
    TestPlanState,
    TestPlanValidationError,
    WorkflowEmbed,
    coerce_state,
    empty_state,
    find_ticket_marker,
    parse_state_blob,
    test_plan_marker,
)
from teatree.core.management.commands._test_plan.workflow_templates import render_browser_click_first, render_link_api
from teatree.utils.url_slug import pr_ref_from_url

_EMPTY_CELL = "—"

__all__ = [
    "DEFAULT_TEMPLATE",
    "KNOWN_TEMPLATES",
    "SideManifest",
    "SideState",
    "TestPlanManifest",
    "TestPlanState",
    "TestPlanValidationError",
    "WorkflowArtifacts",
    "WorkflowEmbed",
    "coerce_state",
    "empty_state",
    "find_ticket_marker",
    "merge_state",
    "parse_manifest",
    "parse_state_blob",
    "render_body",
    "render_mrs_line",
    "test_plan_marker",
    "validate_template",
]


# --- merge ------------------------------------------------------------------


def merge_state(
    prior: TestPlanState,
    *,
    manifest: TestPlanManifest,
    title: str,
    embeds: dict[str, dict[str, WorkflowEmbed]],
) -> TestPlanState:
    """Overlay this run's sides over *prior*, freezing sides not present in *manifest*.

    Steps persist across re-runs: a steps-less re-run preserves the prior steps.
    """
    state: TestPlanState = {
        "ticket": manifest.ticket or prior.get("ticket", ""),
        "title": title,
        "mrs": list(manifest.mrs) if manifest.mrs else list(prior.get("mrs", [])),
        "dev": prior.get("dev", {"commits": {}, "missing_on_dev": [], "workflows": {}}),
        "local": prior.get("local", {"commits": {}, "workflows": {}}),
        "steps": {name: list(steps) for name, steps in prior.get("steps", {}).items()},
        "template": manifest.template,
        "blocked_workflows": dict(prior.get("blocked_workflows", {})),
    }
    if manifest.dev.present:
        state["dev"] = {
            "commits": dict(manifest.dev.commits),
            "missing_on_dev": list(manifest.dev.missing_on_dev),
            "workflows": embeds.get("dev", {}),
        }
    if manifest.local.present:
        state["local"] = {"commits": dict(manifest.local.commits), "workflows": embeds.get("local", {})}
    for name, steps in manifest.steps.items():
        state["steps"][name] = list(steps)
    for name, reason in manifest.blocked_workflows.items():
        state["blocked_workflows"][name] = reason
    return state


# --- render -----------------------------------------------------------------


def _mr_label(ref: str) -> str:
    """``repo!num`` (GitLab) / ``repo#num`` (GitHub) label for *ref*, or *ref* verbatim."""
    parsed = pr_ref_from_url(ref)
    if parsed is None:
        return ref
    repo = parsed.slug.rsplit("/", 1)[-1]
    sep = "!" if parsed.host_kind == "gitlab" else "#"
    return f"{repo}{sep}{parsed.pr_id}"


def render_mrs_line(mrs: tuple[str, ...]) -> str:
    """``Repos & MRs: [repo!n](url), …`` line, or ``""`` when *mrs* is empty."""
    if not mrs:
        return ""
    parts = [f"[{_mr_label(ref)}]({ref})" if pr_ref_from_url(ref) is not None else _mr_label(ref) for ref in mrs]
    return "Repos & MRs: " + ", ".join(parts)


def _commit_base_index(mrs: tuple[str, ...]) -> dict[str, str]:
    """``{repo_short_name: "<base_url>|<host_kind>"}`` derived from the MR/PR URLs."""
    index: dict[str, str] = {}
    for ref in mrs:
        parsed = pr_ref_from_url(ref)
        if parsed is None:
            continue
        host = urlparse(ref).netloc
        scheme = urlparse(ref).scheme or "https"
        short_name = parsed.slug.rsplit("/", 1)[-1]
        index.setdefault(short_name, f"{scheme}://{host}/{parsed.slug}|{parsed.host_kind}")
    return index


def _commit_md(repo: str, sha: str, base_index: dict[str, str]) -> str:
    """``[repo `sha`](url)`` when resolvable, else bare ``repo `sha```."""
    entry = base_index.get(repo)
    if not entry:
        return f"{repo} `{sha}`"
    base, host_kind = entry.rsplit("|", 1)
    commit_path = "commit" if host_kind == "github" else "-/commit"
    return f"[{repo} `{sha}`]({base}/{commit_path}/{sha})"


def _commits_line(label: str, side: SideState, base_index: dict[str, str]) -> str:
    """``"<label>: repo `sha`, …"`` for one side, or ``""`` when no commits."""
    commits = side.get("commits") or {}
    if not commits:
        return ""
    parts = [_commit_md(repo, sha, base_index) for repo, sha in sorted(commits.items())]
    return f"{label}: " + ", ".join(parts)


def _reconcile_line(dev: SideState, local: SideState) -> str:
    """``"Dev ± Local: repo: = …"`` per shared repo, or ``""`` when no overlap."""
    dev_commits = dev.get("commits") or {}
    local_commits = local.get("commits") or {}
    shared = sorted(set(dev_commits) & set(local_commits))
    if not shared:
        return ""
    parts: list[str] = []
    for repo in shared:
        dev_sha, local_sha = dev_commits[repo], local_commits[repo]
        if dev_sha == local_sha:
            parts.append(f"{repo}: = same commit")
        else:
            parts.append(f"{repo}: ≠ dev `{dev_sha}` vs local `{local_sha}`")
    return "Dev ± Local: " + ", ".join(parts)


def _dev_gap_clause(side: SideState) -> str:
    """The ``⚠️ Not yet on dev`` reconciliation clause, or ``""`` when nothing is missing."""
    missing = side.get("missing_on_dev") or []
    if not missing:
        return ""
    return "⚠️ Not yet on dev: " + ", ".join(missing) + " — expected gap."


def _workflow_names(state: TestPlanState) -> list[str]:
    """Ordered union of both sides' media-bearing workflows and the steps-only ones.

    A steps-only manifest (steps recorded, no screenshots/video) keeps its
    workflows in ``state["steps"]``, never in either side — so they must be
    enumerated here too or their steps never render.
    """
    sources = (state["dev"].get("workflows", {}), state["local"].get("workflows", {}), state.get("steps", {}))
    return list(dict.fromkeys(name for source in sources for name in source))


def _cells(side: SideState, workflow: str) -> tuple[str, list[str]]:
    """``(video_cell, image_cells)`` for one side; ``—`` placeholder when absent."""
    wf = side.get("workflows", {}).get(workflow)
    if wf is None:
        return _EMPTY_CELL, []
    return wf.get("video_md") or _EMPTY_CELL, list(wf.get("image_md", []))


def _test_plan_block(state: TestPlanState, workflow: str) -> list[str]:
    """Numbered ``**How to test:**`` step list for *workflow*, or ``[]`` when none."""
    steps = state.get("steps", {}).get(workflow, [])
    if not steps:
        return []
    return ["**How to test:**", "", *[f"{i}. {step}" for i, step in enumerate(steps, start=1)], ""]


def _workflow_table(state: TestPlanState, workflow: str) -> list[str]:
    """Heading + optional steps + ``| Dev | Local |`` table for one workflow.

    A backend-only workflow carries neither video nor screenshots on either
    side; emitting the table header alone renders an empty ``| Dev | Local |``
    grid that reads as missing evidence. Such a workflow renders as its heading
    + steps only (the steps carry the ``Actual: ✅`` claim), no empty table.
    """
    dev_video, dev_images = _cells(state["dev"], workflow)
    local_video, local_images = _cells(state["local"], workflow)

    lines = [f"### {workflow}", ""]
    lines.extend(_test_plan_block(state, workflow))

    has_video = dev_video != _EMPTY_CELL or local_video != _EMPTY_CELL
    has_images = bool(dev_images or local_images)
    if not has_video and not has_images:
        lines.append("")
        return lines

    lines.extend(["| Dev | Local |", "|---|---|"])
    if has_video:
        lines.append(f"| {dev_video} | {local_video} |")
    for i in range(max(len(dev_images), len(local_images))):
        left = dev_images[i] if i < len(dev_images) else _EMPTY_CELL
        right = local_images[i] if i < len(local_images) else _EMPTY_CELL
        lines.append(f"| {left} | {right} |")
    lines.append("")
    return lines


def _render_header(state: TestPlanState) -> list[str]:
    """Shared preamble for every template: markers, heading, MRs, commits, reconcile."""
    ticket_id = state.get("ticket", "")
    title = state.get("title", "") or ticket_id
    dev, local = state["dev"], state["local"]
    base_index = _commit_base_index(tuple(state.get("mrs", [])))
    lines: list[str] = [
        test_plan_marker(ticket_id=ticket_id),
        f"<!-- t3-e2e-data {json.dumps(state, separators=(',', ':'), sort_keys=True)} -->",
        f"## Test Plan — {title}",
        "",
    ]
    mrs_line = render_mrs_line(tuple(state.get("mrs", [])))
    if mrs_line:
        lines.append(mrs_line)
    dev_line = _commits_line("Dev deployed", dev, base_index)
    gap = _dev_gap_clause(dev)
    if dev_line or gap:
        lines.append("  ".join(part for part in (dev_line, gap) if part))
    local_line = _commits_line("Local tested", local, base_index)
    if local_line:
        lines.append(local_line)
    reconcile = _reconcile_line(dev, local)
    if reconcile:
        lines.append(reconcile)
    lines.append("")
    return lines


def _blocked_lines(state: TestPlanState) -> list[str]:
    """Blocked-workflow placeholders: one heading + reason per entry."""
    lines: list[str] = []
    for workflow, reason in (state.get("blocked_workflows") or {}).items():
        lines.extend((f"### {workflow}", "", f"**Blocked:** {reason}", ""))
    return lines


def render_body(state: TestPlanState) -> str:
    """Render the full note body from the merged state.

    Dispatches on ``state["template"]``: ``"browser-click-first"`` →
    numbered steps + inline screenshots; ``"link-api"`` → links + code
    blocks; ``"scenario-plan"`` → per-scenario Preconditions/Steps/Expected/
    Actual blocks with ``---`` separators and an Environment footer; default
    ``"capture-matrix"`` → side-by-side Dev | Local table. The blocked-workflow
    placeholders render on every template (shared tail). Raises
    :class:`TestPlanValidationError` when nothing to post.
    """
    template = state.get("template") or DEFAULT_TEMPLATE
    if template == "browser-click-first":
        workflow_lines = render_browser_click_first(state, workflow_names=_workflow_names)
    elif template == "link-api":
        workflow_lines = render_link_api(state, workflow_names=_workflow_names, test_plan_block=_test_plan_block)
    elif template == "scenario-plan":
        workflow_lines = render_scenario_plan(state)
    else:
        workflow_lines = []
        for workflow in _workflow_names(state):
            workflow_lines.extend(_workflow_table(state, workflow))

    blocked = state.get("blocked_workflows") or {}
    dev_commits = bool(state["dev"].get("commits"))
    local_commits = bool(state["local"].get("commits"))
    mrs = bool(state.get("mrs"))
    scenarios = bool(state.get("scenarios"))
    has_content = bool(_workflow_names(state)) or bool(blocked) or dev_commits or local_commits or mrs or scenarios
    if not has_content:
        msg = "empty: the test-plan state has no workflows and no blocked workflows — nothing to post."
        raise TestPlanValidationError(msg)

    lines = _render_header(state)
    lines.extend(workflow_lines)
    lines.extend(_blocked_lines(state))
    return "\n".join(lines).rstrip("\n") + "\n"
