"""Pure state model, manifest parse, merge, and render for ``e2e post-evidence``.

The string/JSON layer of the one-note-per-ticket evidence model (teatree #272),
split out of ``_e2e_evidence.py`` so the rendering and the host-facing
orchestration each stay focused and under the module-health cap. Nothing here
touches the ORM, the code host, or the CLI — every function is a pure transform
over the manifest input and the persisted :class:`EvidenceState`.

The note carries a hidden machine-readable :class:`EvidenceState` blob
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

# The hidden idempotency marker — keyed on the TICKET (its number, e.g. 8521),
# so a ticket carries exactly ONE evidence note across all environments.
_TICKET_MARKER_RE = re.compile(r"<!--\s*t3-e2e-evidence\s+ticket=(?P<ticket>\S+)\s*-->")
# The hidden machine-readable state blob — the source of truth for the merge.
_DATA_BLOB_RE = re.compile(r"<!--\s*t3-e2e-data\s+(?P<json>\{.*?\})\s*-->", re.DOTALL)


class EvidenceValidationError(ValueError):
    """A pre-post evidence validation failed — the note must NOT be posted.

    Raised by the pure validators below; the command method catches it,
    writes ``str(error)`` to stderr and raises ``SystemExit(1)`` so no
    upload or comment side effect ever runs on invalid evidence.
    """


def _as_dict(value: object) -> Mapping[str, object]:
    """Return *value* as a read-only ``Mapping[str, object]`` when it is a mapping, else ``{}``.

    The narrowing primitive for the untyped JSON manifest/state payloads — a
    ``Mapping`` (not a ``dict``) because every caller only reads from it.
    """
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """Return *value* as a ``list[object]`` when it is a list, else ``[]``."""
    return list(value) if isinstance(value, list) else []


# --- persisted state (the typed source of truth) ----------------------------


class WorkflowEmbed(TypedDict):
    """One workflow's rendered embed markdown for one side: the video + screenshots."""

    video_md: str
    image_md: list[str]


class SideState(TypedDict):
    """One side (dev/local) of the persisted state: commits, workflows, optional gap."""

    commits: dict[str, str]
    workflows: dict[str, WorkflowEmbed]
    missing_on_dev: NotRequired[list[str]]


class EvidenceState(TypedDict):
    """The full persisted note state — serialised into the hidden ``t3-e2e-data`` blob.

    ``steps`` maps a workflow name → its written test-plan steps. It is
    workflow-level (shared across dev/local) so the steps survive any single-side
    re-render, and a steps-less re-run preserves the prior steps (see
    :func:`merge_state`).
    """

    ticket: str
    title: str
    mrs: list[str]
    dev: SideState
    local: SideState
    steps: dict[str, list[str]]


def empty_state(*, ticket: str, title: str) -> EvidenceState:
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
    return {
        "video_md": str(raw_dict.get("video_md") or ""),
        "image_md": [str(i) for i in _as_list(raw_dict.get("image_md"))],
    }


def _coerce_side(raw: object, *, env: str) -> SideState:
    raw_dict = _as_dict(raw)
    commits = {str(k): str(v) for k, v in _as_dict(raw_dict.get("commits")).items()}
    workflows = {str(k): _coerce_workflow(v) for k, v in _as_dict(raw_dict.get("workflows")).items()}
    side: SideState = {"commits": commits, "workflows": workflows}
    if env == "dev":
        side["missing_on_dev"] = [str(m) for m in _as_list(raw_dict.get("missing_on_dev"))]
    return side


def coerce_state(raw: object) -> EvidenceState:
    """Build a well-typed :class:`EvidenceState` from a parsed (untyped) blob.

    The persisted blob is JSON, so it arrives as loose ``object``; this rebuilds
    it into the typed shape, dropping anything malformed (a corrupt blob yields
    an empty state rather than crashing the next run).
    """
    raw_dict = _as_dict(raw)
    return {
        "ticket": str(raw_dict.get("ticket") or ""),
        "title": str(raw_dict.get("title") or ""),
        "mrs": [str(m) for m in _as_list(raw_dict.get("mrs"))],
        "dev": _coerce_side(raw_dict.get("dev"), env="dev"),
        "local": _coerce_side(raw_dict.get("local"), env="local"),
        "steps": _coerce_steps(raw_dict.get("steps")),
    }


def _coerce_steps(raw: object) -> dict[str, list[str]]:
    """Rebuild the workflow → test-plan-steps mapping, dropping malformed entries."""
    return {str(name): [str(s) for s in _as_list(steps)] for name, steps in _as_dict(raw).items() if _as_list(steps)}


# --- parsed manifest (one run's input) --------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowArtifacts:
    """One workflow's captured artifacts for ONE side: name, images, optional video.

    ``images`` and ``video`` are validated on-disk file paths (the video is
    ``None`` when the workflow has no clip on this side). Built from the parsed
    manifest by :func:`_parse_side_workflow`, which validates every referenced
    file exists and is the right media kind.
    """

    workflow: str
    images: tuple[Path, ...]
    video: Path | None = None


@dataclass(frozen=True, slots=True)
class SideManifest:
    """One side's (dev or local) manifest: per-repo commits, gap, per-workflow artifacts.

    ``commits`` maps repo → SHA (the branch SHA tested for ``local``, the
    deployed SHA for ``dev``). ``missing_on_dev`` lists MR refs whose commits
    have not yet deployed (the dev reconciliation line; empty for ``local``).
    ``workflows`` maps workflow name → its artifacts for this side. ``present``
    is False when the run did not carry this side at all (so the merge leaves
    the prior side frozen).
    """

    present: bool
    commits: dict[str, str] = field(default_factory=dict)
    missing_on_dev: tuple[str, ...] = ()
    workflows: dict[str, WorkflowArtifacts] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvidenceManifest:
    """The whole parsed + validated ``--manifest``: ticket, MRs, and per-side input.

    ``steps`` maps a workflow name → its ordered written test-plan steps (the
    "how to test / where to click" list a human follows to reproduce). Steps are
    workflow-level (shared across dev/local), not per-side; a workflow with no
    steps is simply absent from the mapping.
    """

    ticket: str
    mrs: tuple[str, ...]
    dev: SideManifest
    local: SideManifest
    steps: dict[str, tuple[str, ...]] = field(default_factory=dict)


def evidence_marker(*, ticket_id: str) -> str:
    """The hidden HTML-comment idempotency marker keyed on the ticket.

    Renders invisibly in GitLab/GitHub markdown; matched by
    :data:`_TICKET_MARKER_RE` to find the ticket's single evidence note to
    update. Keyed on the ticket number so one note per ticket is maintained
    across every environment.
    """
    return f"<!-- t3-e2e-evidence ticket={ticket_id} -->"


def _mr_label(ref: str) -> str:
    """A terse ``repo!num`` / ``repo#num`` label for an MR ref, falling back to the ref.

    Parses the MR/PR web URL into its repo slug + number; the repo is the last
    slug segment (``group/.../repo`` → ``repo``). GitLab MRs render as
    ``repo!num``, GitHub PRs as ``repo#num``. A ref that does not parse as a
    URL (a bare ``repo!123`` the user typed) is shown verbatim.
    """
    parsed = pr_ref_from_url(ref)
    if parsed is None:
        return ref
    repo = parsed.slug.rsplit("/", 1)[-1]
    sep = "!" if parsed.host_kind == "gitlab" else "#"
    return f"{repo}{sep}{parsed.number}"


def render_mrs_line(mrs: tuple[str, ...]) -> str:
    """Render the terse ``Repos & MRs:`` line with one markdown link per MR.

    A single commit SHA "doesn't mean anything for a multi-repos", so the note
    names each repo's MR at the top. A URL ref renders as ``[repo!num](url)``;
    a non-URL ref renders as its bare label. Returns ``""`` when no MRs were
    given so the caller omits the line entirely.
    """
    if not mrs:
        return ""
    parts = [f"[{_mr_label(ref)}]({ref})" if pr_ref_from_url(ref) is not None else _mr_label(ref) for ref in mrs]
    return "Repos & MRs: " + ", ".join(parts)


def parse_manifest(raw: str) -> EvidenceManifest:
    """Parse the ``--manifest`` JSON into a validated :class:`EvidenceManifest`.

    The manifest is a JSON object carrying the ticket, MRs, the per-side
    ``dev``/``local`` commit metadata, and a ``workflows`` array whose entries
    carry each side's captures::

        {
            "ticket": "8521",
            "mrs": ["...!6331"],
            "dev": {"commits": {"repo": "<sha>"}, "missing_on_dev": ["...!6331 (unmerged)"]},
            "local": {"commits": {"repo": "<sha>"}},
            "workflows": [
                {
                    "workflow": "<name>",
                    "steps": ["open the app", "click Login", "expect the dashboard"],
                    "dev": {"video": null, "images": []},
                    "local": {"video": "v.webm", "images": ["a.png"]}
                }
            ]
        }

    A side is "present" when the manifest carries it (its ``commits`` block or
    any workflow captures for it), so a single-env manifest updates only that
    column. A workflow's optional ``steps`` array is the written test plan (the
    "how to test / where to click" list); it is workflow-level — shared across
    dev/local — and rendered above that workflow's comparison table. Validates
    every referenced file exists and is the right media kind; a missing file or
    wrong kind raises :class:`EvidenceValidationError` so no upload runs on bad
    input.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"--manifest is not valid JSON: {exc}"
        raise EvidenceValidationError(msg) from None
    if not isinstance(data, dict):
        msg = "--manifest must be a JSON object with 'ticket', 'mrs', 'dev'/'local', and 'workflows'."
        raise EvidenceValidationError(msg)
    raw_workflows = data.get("workflows")
    if not isinstance(raw_workflows, list) or not raw_workflows:
        msg = "--manifest 'workflows' must be a non-empty array."
        raise EvidenceValidationError(msg)
    mrs = tuple(str(m).strip() for m in data.get("mrs", []) if str(m).strip())
    sides = {env: _parse_side(data, raw_workflows, env=env) for env in _ENVS}
    if not sides["dev"].present and not sides["local"].present:
        msg = "--manifest carries no 'dev' or 'local' captures; nothing to post."
        raise EvidenceValidationError(msg)
    return EvidenceManifest(
        ticket=str(data.get("ticket", "")).strip(),
        mrs=mrs,
        dev=sides["dev"],
        local=sides["local"],
        steps=_parse_workflow_steps(raw_workflows),
    )


def _parse_workflow_steps(raw_workflows: list[object]) -> dict[str, tuple[str, ...]]:
    """Extract each workflow's optional ``steps`` test plan (workflow-level, shared).

    Returns ``{workflow_name: (step, ...)}`` for every workflow that carries a
    non-empty ``steps`` array; a workflow with no steps is simply absent from the
    mapping (back-compat — its render omits the test-plan block).
    """
    out: dict[str, tuple[str, ...]] = {}
    for entry in raw_workflows:
        entry_dict = _as_dict(entry)
        name = str(entry_dict.get("workflow", "")).strip()
        steps = tuple(str(s).strip() for s in _as_list(entry_dict.get("steps")) if str(s).strip())
        if name and steps:
            out[name] = steps
    return out


def _parse_side(data: Mapping[str, object], raw_workflows: list[object], *, env: str) -> SideManifest:
    """Validate one side (dev/local): its commit block + its per-workflow captures."""
    side_meta = data.get(env)
    workflows = {
        wf.workflow: wf
        for wf in (_parse_side_workflow(entry, env=env, index=i) for i, entry in enumerate(raw_workflows))
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


def _parse_side_workflow(entry: object, *, env: str, index: int) -> WorkflowArtifacts | None:
    """Validate one workflow's captures for *env*, or ``None`` when this side is empty.

    Returns ``None`` (no captures this side) when the workflow object omits the
    side or the side carries neither a video nor any images — so an undeployed
    dev side renders as an empty column rather than a validation error.
    """
    if not isinstance(entry, dict):
        msg = f"--manifest workflow {index} must be an object, got {type(entry).__name__}."
        raise EvidenceValidationError(msg)
    entry_dict = _as_dict(entry)
    workflow = str(entry_dict.get("workflow", "")).strip()
    if not workflow:
        msg = f"--manifest workflow {index} is missing a non-empty 'workflow' name."
        raise EvidenceValidationError(msg)
    if not isinstance(entry_dict.get(env), dict):
        return None
    side = _as_dict(entry_dict.get(env))
    images_list = _as_list(side.get("images"))
    raw_video = side.get("video")
    if not images_list and not raw_video:
        return None
    where = f"{workflow} ({env})"
    images = tuple(_validated_file(str(img), kind=MediaKind.IMAGE, workflow=where) for img in images_list)
    video = _validated_file(str(raw_video), kind=MediaKind.VIDEO, workflow=where) if raw_video else None
    return WorkflowArtifacts(workflow=workflow, images=images, video=video)


def _validated_file(path_str: str, *, kind: MediaKind, workflow: str) -> Path:
    """Confirm *path_str* exists on disk and is *kind*, returning the resolved path."""
    path = Path(path_str)
    if not path.is_file():
        msg = f"workflow {workflow!r}: artifact not found: {path_str}"
        raise EvidenceValidationError(msg)
    if media_kind(path) is not kind:
        msg = f"workflow {workflow!r}: {path.name} is not a recognised {kind.value} file."
        raise EvidenceValidationError(msg)
    return path


# --- merge ------------------------------------------------------------------


def merge_state(
    prior: EvidenceState,
    *,
    manifest: EvidenceManifest,
    title: str,
    embeds: dict[str, dict[str, WorkflowEmbed]],
) -> EvidenceState:
    """Merge THIS run's side(s) over the prior persisted state, returning new state.

    The prior state is the source of truth; this run overwrites only the sides
    it carries (``manifest.dev.present`` / ``manifest.local.present``), so a
    dev-only run freezes ``local`` and vice versa. ``embeds`` holds the freshly
    uploaded embed markdown for this run's workflows, keyed
    ``embeds[env][workflow]``. The title and MRs are refreshed from this run's
    inputs. When the dev commits now include the deployed MR commits, supplying
    an empty ``missing_on_dev`` naturally clears the gap line. The per-workflow
    test-plan ``steps`` are workflow-level and persist across re-runs: this run's
    steps overwrite a workflow's steps, but a steps-less re-run preserves the
    prior steps (a workflow whose steps this run omits keeps what was recorded).
    """
    state: EvidenceState = {
        "ticket": manifest.ticket or prior.get("ticket", ""),
        "title": title,
        "mrs": list(manifest.mrs) if manifest.mrs else list(prior.get("mrs", [])),
        "dev": prior.get("dev", {"commits": {}, "missing_on_dev": [], "workflows": {}}),
        "local": prior.get("local", {"commits": {}, "workflows": {}}),
        "steps": {name: list(steps) for name, steps in prior.get("steps", {}).items()},
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
    return state


# --- render -----------------------------------------------------------------


def _commit_base_index(mrs: tuple[str, ...]) -> dict[str, str]:
    """Map each repo short-name → its project web base URL, derived from the MRs.

    A commit SHA in ``state["commits"]`` is keyed by repo short-name only, so the
    full project path needed for a commit link is recovered by matching that
    short-name against the MR/PR URLs already in the note (``…/<full>/-/merge_requests/<n>``
    → base ``https://<host>/<full>``). Only URL-parseable MRs contribute; a repo
    with no matching MR is absent, so its SHA renders as a bare code-span (never a
    broken link). The web base is forge-shaped: GitLab ``…/-/commit/<sha>`` and
    GitHub ``…/commit/<sha>`` are appended by :func:`_commit_md`.
    """
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
    """Render one ``repo `sha``` cell, as a clickable commit link when resolvable.

    Returns ``[repo `sha`](<base>/-/commit/<sha>)`` (GitLab) /
    ``…/commit/<sha>`` (GitHub) when *repo* has a project base in *base_index*,
    else the bare ``repo `sha``` code-span (never a broken link).
    """
    entry = base_index.get(repo)
    if not entry:
        return f"{repo} `{sha}`"
    base, host_kind = entry.rsplit("|", 1)
    commit_path = "commit" if host_kind == "github" else "-/commit"
    return f"[{repo} `{sha}`]({base}/{commit_path}/{sha})"


def _commits_line(label: str, side: SideState, base_index: dict[str, str]) -> str:
    """The per-repo commit-provenance line for one side, or ``""`` when none.

    Each ``repo `sha``` is a clickable commit link when the repo's project base
    resolves from the note's MRs (see :func:`_commit_base_index`), else a bare
    code-span.
    """
    commits = side.get("commits") or {}
    if not commits:
        return ""
    parts = [_commit_md(repo, sha, base_index) for repo, sha in sorted(commits.items())]
    return f"{label}: " + ", ".join(parts)


def _reconcile_line(dev: SideState, local: SideState) -> str:
    """The per-repo Dev↔Local ``±`` reconciliation line, or ``""`` when no repo overlaps.

    For each repo present on BOTH sides: ``repo: = same commit`` when the dev and
    local SHAs match, else ``repo: ≠ dev `<sha>` vs local `<sha>``` — so "are dev
    and local on the same commit?" is explicit and obvious per repo.
    """
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


def _workflow_names(state: EvidenceState) -> list[str]:
    """The ordered union of workflow names across both sides (dev order, then new local)."""
    names: list[str] = []
    for side in (state["dev"], state["local"]):
        for name in side.get("workflows", {}):
            if name not in names:
                names.append(name)
    return names


def _cells(side: SideState, workflow: str) -> tuple[str, list[str]]:
    """Return ``(video_cell, image_cells)`` for one side of a workflow.

    A missing workflow or a missing video yields the ``—`` placeholder so the
    column stays aligned (e.g. dev not yet deployed).
    """
    wf = side.get("workflows", {}).get(workflow)
    if wf is None:
        return _EMPTY_CELL, []
    return wf.get("video_md") or _EMPTY_CELL, list(wf.get("image_md", []))


def _test_plan_block(state: EvidenceState, workflow: str) -> list[str]:
    """Render the ``**How to test:**`` numbered step list for one workflow, or ``[]``.

    The written test plan a human follows to reproduce the workflow manually,
    rendered ABOVE the workflow's comparison table. Omitted entirely (back-compat)
    when the workflow has no recorded steps.
    """
    steps = state.get("steps", {}).get(workflow, [])
    if not steps:
        return []
    return ["**How to test:**", "", *[f"{i}. {step}" for i, step in enumerate(steps, start=1)], ""]


def _workflow_table(state: EvidenceState, workflow: str) -> list[str]:
    """Render one workflow's block: heading, test-plan steps, then the ``| Dev | Local |`` table.

    The optional ``**How to test:**`` numbered step list (the written test plan)
    renders above the table. Table row 1 = each side's video (``—`` when absent);
    then one row per screenshot pair (dev capture left, local capture right; ``—``
    where a side has fewer captures — e.g. dev not deployed yet, all ``—``).
    """
    dev_video, dev_images = _cells(state["dev"], workflow)
    local_video, local_images = _cells(state["local"], workflow)

    lines = [f"### {workflow}", ""]
    lines.extend(_test_plan_block(state, workflow))
    lines.extend(["| Dev | Local |", "|---|---|", f"| {dev_video} | {local_video} |"])
    for i in range(max(len(dev_images), len(local_images))):
        left = dev_images[i] if i < len(dev_images) else _EMPTY_CELL
        right = local_images[i] if i < len(local_images) else _EMPTY_CELL
        lines.append(f"| {left} | {right} |")
    lines.append("")
    return lines


def render_body(state: EvidenceState) -> str:
    """Render the full evidence note body from the merged state.

    Layout: the hidden ticket marker, the hidden ``t3-e2e-data`` JSON blob (the
    source of truth for the next run's merge), the ``## E2E Evidence — <title>``
    heading, the ``Repos & MRs:`` line, the per-side ``Dev deployed`` / ``Local
    tested`` commit-provenance lines (each ``repo `sha``` a clickable commit link
    when resolvable; the dev line carrying the ``⚠️ Not yet on dev`` gap clause
    when MRs are unmerged), the ``Dev ± Local`` reconciliation line (same / differ
    per shared repo), then per workflow: its heading, the optional
    ``**How to test:**`` numbered test-plan steps, and the side-by-side ``Dev |
    Local`` comparison table.
    """
    ticket_id = state.get("ticket", "")
    title = state.get("title", "") or ticket_id
    dev, local = state["dev"], state["local"]
    base_index = _commit_base_index(tuple(state.get("mrs", [])))

    lines = [
        evidence_marker(ticket_id=ticket_id),
        f"<!-- t3-e2e-data {json.dumps(state, separators=(',', ':'), sort_keys=True)} -->",
        f"## E2E Evidence — {title}",
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
    for workflow in _workflow_names(state):
        lines.extend(_workflow_table(state, workflow))
    return "\n".join(lines).rstrip("\n") + "\n"


def parse_state_blob(body: str) -> EvidenceState:
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
    """True iff *body* carries this ticket's evidence marker."""
    match = _TICKET_MARKER_RE.search(body)
    return match is not None and match.group("ticket") == ticket_id
