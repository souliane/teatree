"""Captures a plan carries in git, and the gate over them.

A plan normally CITES its captures by their artifacts-root-relative path, so no
binaries enter the repo. A plan issued outside the repository cannot do that —
its readers have no access to that artifacts directory — so those captures are
committed beside it under ``test-plans/evidence/<plan name>/<env>/`` and embedded
by a relative link. Each side owns its own directory, so a local and a stack
capture of the same slot never overwrite each other. Captures committed flat,
before sides had directories, migrate to their recorded side when it is safe
to do so; a flat capture still cited by another side remains in place.

That exception is where evidence stopped being validated: the citation path runs
the preflight over the manifest's files, but a capture placed in the evidence
directory by hand never passed one. So this module owns both halves — it is the
only way to put a capture there, and it re-runs
:func:`~teatree.core.evidence.test_plan_validation.validate_test_plan_images`
over everything already there before a run may write. A committed capture with
no ``highlightAndShoot`` red box, or a duplicate of another, refuses the write by
name; ``skip`` is the same user-authorised bypass the manifest preflight carries.
"""

import re
import shutil
from collections.abc import Collection
from pathlib import Path

from teatree.core.evidence.bdd_scenario_source import BddSourceError, validate_bdd_source
from teatree.core.evidence.test_plan_validation import TestPlanImageValidationError, validate_test_plan_images
from teatree.core.invocation_cwd import INVOCATION_CWD_ENV, declared_invocation_cwd
from teatree.core.management.commands._test_plan.file_store import PLAN_DIR_NAME
from teatree.core.management.commands._test_plan.render import SideManifest, WorkflowEmbed
from teatree.core.management.commands._test_plan.state import PlanState, TestPlanValidationError

EVIDENCE_DIR_NAME = "evidence"
PLANS_DIR_DEFAULT = PLAN_DIR_NAME
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})

__all__ = [
    "EVIDENCE_DIR_NAME",
    "PLANS_DIR_DEFAULT",
    "committed_captures",
    "embed_side_captures",
    "evidence_dir_for",
    "legacy_flat_capture_migration",
    "migrate_legacy_flat_captures",
    "refuse_invalid_committed_captures",
    "resolve_default_plans_dir",
    "verify_plans_dir",
]


def resolve_default_plans_dir() -> Path:
    """``test-plans`` under the directory the operator invoked ``t3`` from, or a loud refusal.

    Under the containerized CLI the process cwd is the image WORKDIR, a tree the
    operator never named — so defaulting to it reported THEIR captures missing,
    which invites a fix to a plan that was never broken. The refusal names the
    directory and the seam it came from, and is worded so a reader can tell it
    from :func:`verify_plans_dir`'s verdict on an evidence tree that really is empty.
    """
    declared = declared_invocation_cwd()
    cwd = declared or Path.cwd()
    root = cwd / PLANS_DIR_DEFAULT
    if root.is_dir():
        return root
    origin = INVOCATION_CWD_ENV if declared else "the process working directory — nothing declared an invocation cwd"
    msg = (
        f"Cannot resolve a plans directory: {root} does not exist. The default is "
        f"{PLANS_DIR_DEFAULT}/ under {cwd}, which came from {origin}. No capture was "
        f"looked at — pass --plans-dir to name the directory yourself."
    )
    raise TestPlanValidationError(msg)


def evidence_dir_for(plan_path: Path) -> Path:
    """``<plan dir>/evidence/<plan name>/`` — the captures this plan may carry in git.

    Keyed off the plan's own filename rather than the bare work-item number,
    which repeats across repos: two same-numbered tickets would otherwise share
    one directory and overwrite each other's captures by name.
    """
    return plan_path.parent / EVIDENCE_DIR_NAME / plan_path.stem


def committed_captures(evidence_dir: Path) -> list[Path]:
    """Every image already committed under *evidence_dir*, sorted; ``[]`` when absent."""
    if not evidence_dir.is_dir():
        return []
    return sorted(p for p in evidence_dir.rglob("*") if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES)


def legacy_flat_capture_migration(
    evidence_dir: Path, *, incoming: dict[str, list[Path]], prior: PlanState
) -> dict[Path, Path]:
    """Map each flat capture that only the re-captured side cites to that side's directory.

    A flat capture another side still cites stays put, so an identical re-capture meets it
    in the duplicate gate as the cross-side duplicate it is.
    """
    migrations: dict[Path, Path] = {}
    for env, captures in incoming.items():
        for capture in captures:
            legacy = evidence_dir / capture.name
            citation = f"{EVIDENCE_DIR_NAME}/{evidence_dir.name}/{capture.name}"
            if legacy.is_file() and _legacy_capture_sides(prior, citation=citation) == {env}:
                migrations[legacy] = evidence_dir / env / capture.name
    return migrations


def migrate_legacy_flat_captures(migrations: dict[Path, Path], *, prior: PlanState) -> None:
    """Move safe legacy captures and update their persisted plan citations."""
    for legacy, destination in migrations.items():
        legacy_citation = f"{EVIDENCE_DIR_NAME}/{legacy.parent.name}/{legacy.name}"
        destination_citation = (
            f"{EVIDENCE_DIR_NAME}/{destination.parent.parent.name}/{destination.parent.name}/{destination.name}"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(destination)
        _rewrite_capture_citation(prior, old=legacy_citation, new=destination_citation)


def refuse_invalid_committed_captures(
    evidence_dir: Path,
    *,
    incoming: dict[str, list[Path]],
    superseded_legacy: Collection[Path] = (),
    skip: bool = False,
) -> None:
    """Refuse the run when the captures this plan will carry in git are not evidence.

    Validates the set the repo holds AFTER this run — the already-committed
    captures this run does not replace, plus the ones it is about to write —
    so a re-capture that replaces every stale image passes while a leftover is
    named. The duplicate check spans every side's directory and legacy flat
    captures, except a legacy capture this run migrates. Raises
    :class:`TestPlanValidationError`; nothing is copied or written.
    """
    destinations = {evidence_dir / env / path.name for env, paths in incoming.items() for path in paths}
    resulting = [
        path for path in committed_captures(evidence_dir) if path not in destinations and path not in superseded_legacy
    ]
    resulting.extend(path for paths in incoming.values() for path in paths)
    try:
        validate_test_plan_images(resulting, skip=skip)
    except TestPlanImageValidationError as exc:
        msg = f"{exc} (captures committed under {evidence_dir})"
        raise TestPlanValidationError(msg) from exc


def _legacy_capture_sides(prior: PlanState, *, citation: str) -> set[str]:
    """The plan-state sides whose workflow embeds cite a legacy capture."""
    return {
        env
        for env in ("dev", "local", "stack")
        if any(_workflow_cites(workflow, citation=citation) for workflow in prior[env]["workflows"].values())
    }


def _workflow_cites(workflow: WorkflowEmbed, *, citation: str) -> bool:
    """Whether a workflow embed refers to *citation*."""
    return citation in workflow["video_md"] or any(citation in image for image in workflow["image_md"])


def _rewrite_capture_citation(prior: PlanState, *, old: str, new: str) -> None:
    """Replace a migrated capture's persisted image or video embed."""
    for env in ("dev", "local", "stack"):
        for workflow in prior[env]["workflows"].values():
            workflow["video_md"] = workflow["video_md"].replace(old, new)
            workflow["image_md"] = [image.replace(old, new) for image in workflow["image_md"]]


def verify_plans_dir(plans_dir: Path, *, skip: bool = False) -> list[str]:
    """Validate every ticket's committed captures under *plans_dir*; return the failures.

    The standing check a repo runs over what is already in git — the half a
    command cannot own, because a capture can be committed without running one.
    Raises :class:`TestPlanValidationError` when the ``evidence`` directory does
    not exist, and equally when it holds no image at all: a check that reports
    success because it found nothing to look at is the failure it exists to
    prevent, and an empty tree looks exactly like a clean one.
    """
    evidence_root = plans_dir / EVIDENCE_DIR_NAME
    if not evidence_root.is_dir():
        msg = f"No committed captures to verify: {evidence_root} does not exist."
        raise TestPlanValidationError(msg)
    plan_dirs = sorted(path for path in evidence_root.iterdir() if path.is_dir())
    per_plan = {plan_dir: committed_captures(plan_dir) for plan_dir in plan_dirs}
    if not any(per_plan.values()):
        msg = f"No committed captures to verify: {evidence_root} holds no image."
        raise TestPlanValidationError(msg)
    failures: list[str] = []
    for plan_dir, captures in per_plan.items():
        failures.extend(_scenario_source_failures(plans_dir, plan_dir.name))
        try:
            validate_test_plan_images(captures, skip=skip)
        except TestPlanImageValidationError as exc:
            failures.append(f"{plan_dir.name}: {exc}")
    return failures


def _owning_plans(plans_dir: Path, name: str) -> list[Path]:
    owner = re.compile(rf"{re.escape(name)}(?:-[a-z]{{2}})?\.md")
    return sorted(path for path in plans_dir.iterdir() if path.is_file() and owner.fullmatch(path.name))


def _scenario_source_failures(plans_dir: Path, name: str) -> list[str]:
    owners = _owning_plans(plans_dir, name)
    if not owners:
        return [f"{name}: no plan owns these captures — expected {name}.md or {name}-<lang>.md"]
    failures: list[str] = []
    for owner in owners:
        try:
            validate_bdd_source(owner.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(f"{owner.stem}: cannot read {owner.name}: {exc}")
        except BddSourceError as exc:
            failures.append(f"{owner.stem}: {exc}")
    return failures


def embed_side_captures(evidence_dir: Path, *, env: str, side: SideManifest) -> dict[str, WorkflowEmbed]:
    """Copy one side's captures into ``<evidence_dir>/<env>/``, returning their relative embeds.

    The embed is a link relative to the plan file, so the document renders in a
    forge blob view, in an editor, and in a copy sent to someone with no access
    to this repository. Two captures sharing a file name within one run are
    refused: the duplicate gate already proved they differ, so one would
    silently overwrite the other.
    """
    destination = evidence_dir / env
    relative_dir = f"{evidence_dir.name}/{env}"
    written: dict[str, Path] = {}
    embeds: dict[str, WorkflowEmbed] = {}
    for name, workflow in side.workflows.items():
        video_md = ""
        if workflow.video is not None:
            video_md = _copy_one(
                destination,
                source=workflow.video,
                label=f"{name} — video",
                relative_dir=relative_dir,
                written=written,
            )
        image_md = [
            _copy_one(
                destination,
                source=image,
                label=f"{name} — {image.stem}",
                relative_dir=relative_dir,
                written=written,
            )
            for image in workflow.images
        ]
        embeds[name] = {"video_md": video_md, "image_md": image_md}
    return embeds


def _copy_one(destination: Path, *, source: Path, label: str, relative_dir: str, written: dict[str, Path]) -> str:
    """Copy one capture into *destination* and return its plan-relative embed markdown."""
    clash = written.get(source.name)
    if clash is not None and clash != source:
        msg = (
            f"Two captures in this run are both named {source.name!r} ({clash} and {source}) — "
            f"one would overwrite the other under {destination}. Rename one before re-running."
        )
        raise TestPlanValidationError(msg)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination / source.name)
    written[source.name] = source
    return f"![{label}]({EVIDENCE_DIR_NAME}/{relative_dir}/{source.name})"
