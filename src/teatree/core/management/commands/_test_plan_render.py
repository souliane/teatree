"""Pure state model, manifest parse, merge, and render for ``e2e post-test-plan``.

The string/JSON layer of the one-note-per-ticket test-plan model (teatree #272),
split out of ``_test_plan.py`` so the rendering and the host-facing
orchestration each stay focused and under the module-health cap. Nothing here
touches the ORM, the code host, or the CLI — every function is a pure transform
over the manifest input and the persisted :class:`TestPlanState`.

The note carries a hidden machine-readable :class:`TestPlanState` blob
(``<!-- t3-e2e-data {…} -->``) that is the source of truth. :func:`merge_state`
overlays one run's side(s) on the prior state (freezing the side the run does
not carry); :func:`render_body` is a pure function of the merged state.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import urlparse

from teatree.utils.media import MediaKind, media_kind
from teatree.utils.url_slug import pr_ref_from_url

# The two columns of every workflow table. Dev on the LEFT, Local on the RIGHT.
_ENVS = ("dev", "local")
_EMPTY_CELL = "—"

# The known body templates; the default is the side-by-side capture matrix.
DEFAULT_TEMPLATE = "capture-matrix"
KNOWN_TEMPLATES = (DEFAULT_TEMPLATE, "browser-click-first", "link-api")

# The hidden idempotency marker — keyed on the TICKET (its number, e.g. 8521),
# so a ticket carries exactly ONE test-plan note across all environments.
#
# The emitted string stays ``t3-e2e-evidence`` (NOT renamed to ``t3-test-plan``)
# on purpose: it is PERSISTED in live GitLab/GitHub ticket notes. Changing the
# emitted marker would break idempotent update of every note posted before this
# rename — a fresh note would be created beside the stale one. The regex below
# is therefore the durable wire format; the concept rename is user-facing only.
_TICKET_MARKER_RE = re.compile(r"<!--\s*t3-e2e-evidence\s+ticket=(?P<ticket>\S+)\s*-->")
# The hidden machine-readable state blob — the source of truth for the merge.
_DATA_BLOB_RE = re.compile(r"<!--\s*t3-e2e-data\s+(?P<json>\{.*?\})\s*-->", re.DOTALL)


class TestPlanValidationError(ValueError):
    """Raised when a manifest fails pre-post validation; the note is NOT posted."""


def _as_dict(value: object) -> Mapping[str, object]:
    """``value`` as a read-only mapping when it is a dict, else ``{}``."""
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """Return *value* as a ``list[object]`` when it is a list, else ``[]``."""
    return list(value) if isinstance(value, list) else []


# --- persisted state (the typed source of truth) ----------------------------


class WorkflowEmbed(TypedDict):
    """One workflow's rendered embed markdown for one side: the video + screenshots."""

    video_md: str
    image_md: list[str]
    link_md: NotRequired[str]
    code_md: NotRequired[str]


class SideState(TypedDict):
    """One side (dev/local) of the persisted state: commits, workflows, optional gap."""

    commits: dict[str, str]
    workflows: dict[str, WorkflowEmbed]
    missing_on_dev: NotRequired[list[str]]


class TestPlanState(TypedDict):
    """The full persisted note state — serialised into the hidden ``t3-e2e-data`` blob.

    ``template``: ``"capture-matrix"`` (default), ``"browser-click-first"``, or
    ``"link-api"``. ``steps``: workflow → ordered step list (shared across sides,
    persisted across re-runs). ``blocked_workflows``: workflow → reason string.
    """

    ticket: str
    title: str
    mrs: list[str]
    dev: SideState
    local: SideState
    steps: dict[str, list[str]]
    template: NotRequired[str]
    blocked_workflows: NotRequired[dict[str, str]]


def empty_state(*, ticket: str, title: str) -> TestPlanState:
    """A fresh state with both sides empty."""
    return {
        "ticket": ticket,
        "title": title,
        "mrs": [],
        "dev": {"commits": {}, "missing_on_dev": [], "workflows": {}},
        "local": {"commits": {}, "workflows": {}},
        "steps": {},
    }


def _coerce_workflow(raw: object) -> WorkflowEmbed:
    raw_dict = _as_dict(raw)
    embed: WorkflowEmbed = {
        "video_md": str(raw_dict.get("video_md") or ""),
        "image_md": [str(i) for i in _as_list(raw_dict.get("image_md"))],
    }
    if raw_dict.get("link_md"):
        embed["link_md"] = str(raw_dict["link_md"])
    if raw_dict.get("code_md"):
        embed["code_md"] = str(raw_dict["code_md"])
    return embed


def _coerce_side(raw: object, *, env: str) -> SideState:
    raw_dict = _as_dict(raw)
    commits = {str(k): str(v) for k, v in _as_dict(raw_dict.get("commits")).items()}
    workflows = {str(k): _coerce_workflow(v) for k, v in _as_dict(raw_dict.get("workflows")).items()}
    side: SideState = {"commits": commits, "workflows": workflows}
    if env == "dev":
        side["missing_on_dev"] = [str(m) for m in _as_list(raw_dict.get("missing_on_dev"))]
    return side


def coerce_state(raw: object) -> TestPlanState:
    """Build a well-typed :class:`TestPlanState` from a JSON blob; drops malformed fields."""
    raw_dict = _as_dict(raw)
    state: TestPlanState = {
        "ticket": str(raw_dict.get("ticket") or ""),
        "title": str(raw_dict.get("title") or ""),
        "mrs": [str(m) for m in _as_list(raw_dict.get("mrs"))],
        "dev": _coerce_side(raw_dict.get("dev"), env="dev"),
        "local": _coerce_side(raw_dict.get("local"), env="local"),
        "steps": _coerce_steps(raw_dict.get("steps")),
    }
    template = str(raw_dict.get("template") or "").strip()
    if template in KNOWN_TEMPLATES:
        state["template"] = template
    blocked = _coerce_blocked_workflows(raw_dict.get("blocked_workflows"))
    if blocked:
        state["blocked_workflows"] = blocked
    return state


def _coerce_blocked_workflows(raw: object) -> dict[str, str]:
    """Rebuild the workflow → blocked-reason mapping, dropping malformed entries."""
    return {str(name): str(reason) for name, reason in _as_dict(raw).items() if str(name) and str(reason)}


def _coerce_steps(raw: object) -> dict[str, list[str]]:
    """Rebuild the workflow → test-plan-steps mapping, dropping malformed entries."""
    return {str(name): [str(s) for s in _as_list(steps)] for name, steps in _as_dict(raw).items() if _as_list(steps)}


# --- parsed manifest (one run's input) --------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowArtifacts:
    """Validated on-disk artifact paths for one workflow on one side."""

    workflow: str
    images: tuple[Path, ...]
    video: Path | None = None


@dataclass(frozen=True, slots=True)
class SideManifest:
    """One side (dev/local): commits, gap, per-workflow artifacts.

    ``present`` is False when the run did not carry this side (merge freezes it).
    """

    present: bool
    commits: dict[str, str] = field(default_factory=dict)
    missing_on_dev: tuple[str, ...] = ()
    workflows: dict[str, WorkflowArtifacts] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TestPlanManifest:
    """Parsed + validated ``--manifest``: ticket, MRs, per-side input, template, optional steps."""

    ticket: str
    mrs: tuple[str, ...]
    dev: SideManifest
    local: SideManifest
    steps: dict[str, tuple[str, ...]] = field(default_factory=dict)
    template: str = DEFAULT_TEMPLATE
    blocked_workflows: dict[str, str] = field(default_factory=dict)


def test_plan_marker(*, ticket_id: str) -> str:
    """Hidden HTML-comment idempotency marker; matched by :data:`_TICKET_MARKER_RE`."""
    return f"<!-- t3-e2e-evidence ticket={ticket_id} -->"


def _mr_label(ref: str) -> str:
    """``repo!num`` (GitLab) / ``repo#num`` (GitHub) label for *ref*, or *ref* verbatim."""
    parsed = pr_ref_from_url(ref)
    if parsed is None:
        return ref
    repo = parsed.slug.rsplit("/", 1)[-1]
    sep = "!" if parsed.host_kind == "gitlab" else "#"
    return f"{repo}{sep}{parsed.number}"


def render_mrs_line(mrs: tuple[str, ...]) -> str:
    """``Repos & MRs: [repo!n](url), …`` line, or ``""`` when *mrs* is empty."""
    if not mrs:
        return ""
    parts = [f"[{_mr_label(ref)}]({ref})" if pr_ref_from_url(ref) is not None else _mr_label(ref) for ref in mrs]
    return "Repos & MRs: " + ", ".join(parts)


def parse_manifest(raw: str, *, base_dir: Path | None = None) -> TestPlanManifest:
    """Parse the ``--manifest`` JSON into a validated :class:`TestPlanManifest`.

    Validates every artifact path exists and is the right media kind; raises
    :class:`TestPlanValidationError` on bad input so no upload runs. ``base_dir``
    is the manifest file's directory (relative paths resolve against it).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"--manifest is not valid JSON: {exc}"
        raise TestPlanValidationError(msg) from None
    if not isinstance(data, dict):
        msg = "--manifest must be a JSON object with 'ticket', 'mrs', 'dev'/'local', and 'workflows'."
        raise TestPlanValidationError(msg)
    raw_workflows = data.get("workflows")
    if not isinstance(raw_workflows, list) or not raw_workflows:
        msg = "--manifest 'workflows' must be a non-empty array."
        raise TestPlanValidationError(msg)
    mrs = tuple(str(m).strip() for m in data.get("mrs", []) if str(m).strip())
    template = _parse_template(data.get("template"))
    sides = {env: _parse_side(data, raw_workflows, env=env, base_dir=base_dir) for env in _ENVS}
    if not sides["dev"].present and not sides["local"].present:
        msg = "--manifest carries no 'dev' or 'local' captures; nothing to post."
        raise TestPlanValidationError(msg)
    steps = _parse_workflow_steps(raw_workflows)
    blocked_workflows = _parse_blocked_workflows(data.get("blocked_workflows"))
    has_media = any(sides[env].workflows for env in _ENVS if sides[env].present)
    if not has_media and not steps and not blocked_workflows:
        msg = "--manifest carries no media (no screenshots or video); nothing to post."
        raise TestPlanValidationError(msg)
    return TestPlanManifest(
        ticket=str(data.get("ticket", "")).strip(),
        mrs=mrs,
        dev=sides["dev"],
        local=sides["local"],
        steps=steps,
        template=template,
        blocked_workflows=blocked_workflows,
    )


def validate_template(template: str) -> str:
    """Return *template* if it names a known body template, else raise."""
    if template not in KNOWN_TEMPLATES:
        msg = f"--manifest 'template' must be one of {', '.join(KNOWN_TEMPLATES)}; got {template!r}."
        raise TestPlanValidationError(msg)
    return template


def _parse_template(raw: object) -> str:
    """The validated body template from the manifest, defaulting to the capture matrix."""
    template = str(raw).strip() if raw else ""
    return validate_template(template) if template else DEFAULT_TEMPLATE


def _parse_blocked_workflows(raw: object) -> dict[str, str]:
    """``{workflow: reason}`` for every blocked entry carrying a non-empty reason."""
    out: dict[str, str] = {}
    for name, reason in _as_dict(raw).items():
        if str(name).strip() and str(reason).strip():
            out[str(name).strip()] = str(reason).strip()
    return out


def _parse_workflow_steps(raw_workflows: list[object]) -> dict[str, tuple[str, ...]]:
    """``{workflow: (step, …)}`` for every workflow carrying a non-empty ``steps`` array."""
    out: dict[str, tuple[str, ...]] = {}
    for entry in raw_workflows:
        entry_dict = _as_dict(entry)
        name = str(entry_dict.get("workflow", "")).strip()
        steps = tuple(str(s).strip() for s in _as_list(entry_dict.get("steps")) if str(s).strip())
        if name and steps:
            out[name] = steps
    return out


def _parse_side(
    data: Mapping[str, object], raw_workflows: list[object], *, env: str, base_dir: Path | None
) -> SideManifest:
    """Validate one side (dev/local): its commit block + its per-workflow captures."""
    side_meta = data.get(env)
    workflows = {
        wf.workflow: wf
        for wf in (
            _parse_side_workflow(entry, env=env, index=i, base_dir=base_dir) for i, entry in enumerate(raw_workflows)
        )
        if wf is not None
    }
    has_meta = isinstance(side_meta, dict)
    if not has_meta and not workflows:
        return SideManifest(present=False)
    meta = _as_dict(side_meta)
    commits = {str(k): str(v) for k, v in _as_dict(meta.get("commits")).items()}
    missing: tuple[str, ...] = ()
    if env == "dev":
        missing = tuple(str(m).strip() for m in _as_list(meta.get("missing_on_dev")) if str(m).strip())
    return SideManifest(present=True, commits=commits, missing_on_dev=missing, workflows=workflows)


def _parse_side_workflow(entry: object, *, env: str, index: int, base_dir: Path | None) -> WorkflowArtifacts | None:
    """Validated artifacts for *env*, or ``None`` when this side has no captures."""
    if not isinstance(entry, dict):
        msg = f"--manifest workflow {index} must be an object, got {type(entry).__name__}."
        raise TestPlanValidationError(msg)
    entry_dict = _as_dict(entry)
    workflow = str(entry_dict.get("workflow", "")).strip()
    if not workflow:
        msg = f"--manifest workflow {index} is missing a non-empty 'workflow' name."
        raise TestPlanValidationError(msg)
    if not isinstance(entry_dict.get(env), dict):
        return None
    side = _as_dict(entry_dict.get(env))
    images_list = _as_list(side.get("images"))
    raw_video = side.get("video")
    if not images_list and not raw_video:
        return None
    where = f"{workflow} ({env})"
    images = tuple(
        _validated_file(str(img), kind=MediaKind.IMAGE, workflow=where, base_dir=base_dir) for img in images_list
    )
    video = (
        _validated_file(str(raw_video), kind=MediaKind.VIDEO, workflow=where, base_dir=base_dir) if raw_video else None
    )
    return WorkflowArtifacts(workflow=workflow, images=images, video=video)


def _validated_file(path_str: str, *, kind: MediaKind, workflow: str, base_dir: Path | None) -> Path:
    """Confirm *path_str* exists on disk and is *kind*; resolve a relative path against *base_dir*."""
    path = Path(path_str)
    if base_dir is not None and not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        msg = f"workflow {workflow!r}: artifact not found: {path}"
        raise TestPlanValidationError(msg)
    if media_kind(path) is not kind:
        msg = f"workflow {workflow!r}: {path.name} is not a recognised {kind.value} file."
        raise TestPlanValidationError(msg)
    return path


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
    """Heading + optional steps + ``| Dev | Local |`` table for one workflow."""
    dev_video, dev_images = _cells(state["dev"], workflow)
    local_video, local_images = _cells(state["local"], workflow)

    lines = [f"### {workflow}", ""]
    lines.extend(_test_plan_block(state, workflow))
    lines.extend(["| Dev | Local |", "|---|---|"])
    if dev_video != _EMPTY_CELL or local_video != _EMPTY_CELL:
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


def _render_browser_click_first(state: TestPlanState) -> list[str]:
    """``browser-click-first`` template: numbered steps then inline screenshots."""
    lines: list[str] = []
    for workflow in _workflow_names(state):
        lines.extend((f"### {workflow}", ""))
        steps = state.get("steps", {}).get(workflow, [])
        if steps:
            lines.extend(("**How to test:**", ""))
            lines.extend(f"{i}. {step}" for i, step in enumerate(steps, start=1))
            lines.append("")
        for side in (state["dev"], state["local"]):
            lines.extend(side.get("workflows", {}).get(workflow, {}).get("image_md", []))
        lines.append("")
    return lines


def _render_link_api(state: TestPlanState) -> list[str]:
    """``link-api`` template: How-to-test steps then link_md + code_md per workflow, no table, no images."""
    lines: list[str] = []
    for workflow in _workflow_names(state):
        lines.extend((f"### {workflow}", ""))
        lines.extend(_test_plan_block(state, workflow))
        for side in (state["dev"], state["local"]):
            embed = side.get("workflows", {}).get(workflow, {})
            link_md = embed.get("link_md", "") if embed else ""
            code_md = embed.get("code_md", "") if embed else ""
            if link_md:
                lines.append(link_md)
            if code_md:
                lines.append(code_md)
        lines.append("")
    return lines


def render_body(state: TestPlanState) -> str:
    """Render the full note body from the merged state.

    Dispatches on ``state["template"]``: ``"browser-click-first"`` →
    numbered steps + inline screenshots; ``"link-api"`` → links + code
    blocks; default ``"capture-matrix"`` → side-by-side Dev | Local table.
    The blocked-workflow placeholders render on every template (shared tail).
    Raises :class:`TestPlanValidationError` when nothing to post.
    """
    template = state.get("template") or DEFAULT_TEMPLATE
    if template == "browser-click-first":
        workflow_lines = _render_browser_click_first(state)
    elif template == "link-api":
        workflow_lines = _render_link_api(state)
    else:
        workflow_lines = []
        for workflow in _workflow_names(state):
            workflow_lines.extend(_workflow_table(state, workflow))

    blocked = state.get("blocked_workflows") or {}
    dev_commits = bool(state["dev"].get("commits"))
    local_commits = bool(state["local"].get("commits"))
    mrs = bool(state.get("mrs"))
    has_content = bool(_workflow_names(state)) or bool(blocked) or dev_commits or local_commits or mrs
    if not has_content:
        msg = "empty: the test-plan state has no workflows and no blocked workflows — nothing to post."
        raise TestPlanValidationError(msg)

    lines = _render_header(state)
    lines.extend(workflow_lines)
    lines.extend(_blocked_lines(state))
    return "\n".join(lines).rstrip("\n") + "\n"


def parse_state_blob(body: str) -> TestPlanState:
    """Recover the persisted state from a note body, or an empty state when absent/corrupt."""
    match = _DATA_BLOB_RE.search(body)
    if match is None:
        return empty_state(ticket="", title="")
    try:
        data = json.loads(match.group("json"))
    except json.JSONDecodeError:
        return empty_state(ticket="", title="")
    return coerce_state(data)


def find_ticket_marker(body: str, *, ticket_id: str) -> bool:
    """True iff *body* carries this ticket's test-plan marker."""
    match = _TICKET_MARKER_RE.search(body)
    return match is not None and match.group("ticket") == ticket_id
