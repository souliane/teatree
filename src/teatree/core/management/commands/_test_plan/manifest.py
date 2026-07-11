"""Parse + validate one run's ``--manifest`` input.

The manifest is the CLI-supplied description of one capture run: ticket, MRs,
per-side (dev/local) commit blocks and per-workflow artifacts, optional steps
and blocked-workflow reasons. Every artifact path is validated against disk
and media kind here so :class:`TestPlanValidationError` fires BEFORE any
upload runs. The parsed :class:`TestPlanManifest` is a pure value object the
merge (in :mod:`.render`) overlays onto the persisted state.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.management.commands._test_plan.state import (
    _ENVS,
    DEFAULT_TEMPLATE,
    KNOWN_TEMPLATES,
    TestPlanValidationError,
    _as_dict,
    _as_list,
)
from teatree.utils.media import MediaKind, media_kind


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
